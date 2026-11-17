"""T7: 프롬프트 스위치(LLM_PROMPT_VERSION)와 그림자 모드(ANALYSIS_WEBHOOK_URL).

가짜 웹훅은 로컬 임시 포트의 http 서버 두 개(main, analysis)다. 진짜 Slack 주소는 쓰지 않는다.
Config는 https 주소만 받으므로, 라우팅 시험은 Notifier를 직접 만들어 http://127.0.0.1 주소를 넘긴다.
"""
import hashlib
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_dispatcher as td
import test_eval as t5a  # noqa: F401  (scripts/eval을 import 경로에 넣는다)
from fake_ollama import FakeOllama

import analysis
import config
import main as watcher_main
from config import Config
from notifier import LLM_PENDING, Notifier
from state import Incident
from stats import WindowStats

variants = importlib.import_module("variants")

SYSTEM_PROMPT_V2_SHA256 = "f4575ffd2359e617b29bfe85187b30da10eb125a36b296199e3ff3ca0e8d6af8"
SECRET = "https://SECRET-MARKER.example/x"


# ---------------------------------------------------------------- 도우미

def _inc(id=1, state="FIRING") -> Incident:
    return Incident(id, "visit-counter", state, "critical", ["R1"], "2026-11-02T00:00:00+00:00",
                    "2026-11-02T00:06:00+00:00" if state == "RESOLVED" else None, 1, 0)


def _evidence(n_paths=5, n_kinds=5) -> dict:
    records = []
    for i in range(n_paths):
        records += [{"path": f"/p{i}", "status": 500, "error": f"Kind{i}Error: boom"}] * (10 - i)
    for i in range(n_kinds):
        records += [{"path": "/k", "status": 500, "error": f"Other{i}Error: x"}] * (5 - i)
    return analysis.build_evidence(_inc(), WindowStats(100, 40, 30.0, 100.0), records, 300.0,
                                   {"consecutive_failures": 0, "detail": "HTTP 200"}, 1762041660.0,
                                   current_rules=["R1"])


def _old_build_messages(evidence, runbooks_text):
    """작업 전(origin/main 6d1973a) analysis.build_messages를 그대로 옮긴 참조 구현."""
    system = analysis.SYSTEM_PROMPT_V1 + ("\n\n" + runbooks_text if runbooks_text else "")
    if len(system) + len(analysis.render_evidence(evidence)) > analysis.PROMPT_TOKEN_BUDGET:
        evidence = analysis._shrunk(evidence)
    return [{"role": "system", "content": system},
            {"role": "user", "content": analysis.mask(analysis.render_evidence(evidence))}]


class FakeWebhook:
    """POST 본문을 모으는 로컬 http 서버. with FakeWebhook("/main-hook-path") as h: h.url, h.bodies"""

    def __init__(self, path: str):
        self.path, self.bodies = path, []
        hook = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                hook.bodies.append((self.path, json.loads(self.rfile.read(n))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{self.path}"

    def kinds(self) -> list[str]:
        return [b["text"].split("]")[0].lstrip("[") for _, b in self.bodies]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        return False


ALL_KINDS = ("FIRING", "UPDATE", "RESOLVED", "ANALYSIS", "ANALYSIS_FAILED")


def _send_all(n: Notifier):
    for kind in ALL_KINDS:
        inc = _inc(state="RESOLVED") if kind == "RESOLVED" else _inc()
        ctx = {"text": "분석 본문"} if kind.startswith("ANALYSIS") else {}
        assert n.send(kind, inc, **ctx) is True


def _description(payload) -> str:
    blocks = payload["attachments"][0]["blocks"]
    return next((b["text"]["text"] for b in blocks if b["type"] == "section" and "text" in b), "")


# ---------------------------------------------------------------- 1) 프롬프트

def test_system_prompt_v2_is_pinned_and_matches_eval():
    assert hashlib.sha256(analysis.SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest() == SYSTEM_PROMPT_V2_SHA256
    assert analysis.SYSTEM_PROMPT_V2 == variants.SYSTEM_PROMPT_V2
    assert analysis.PROMPT_VERSIONS == ("v1", "v2") and analysis.PROMPT_VERSION == "v1"


@pytest.mark.parametrize("bad", ["v3", "", "V1", None])
def test_unknown_version_raises(bad):
    with pytest.raises(ValueError):
        analysis.build_messages(_evidence(), "", bad)


@pytest.mark.parametrize("rb_len", [0, 50, 1500, 2600, 4000])
def test_v1_messages_are_byte_identical_to_before(rb_len):
    ev, rb = _evidence(), "[런북] 시험\n" + "가" * rb_len if rb_len else ""
    old = _old_build_messages(ev, rb)
    assert analysis.build_messages(ev, rb) == old
    assert analysis.build_messages(ev, rb, "v1") == old
    assert json.dumps(analysis.build_messages(ev, rb, "v1"), ensure_ascii=False).encode() == \
        json.dumps(old, ensure_ascii=False).encode()


def test_v2_message_shape():
    ev, rb = _evidence(), "[런북] 시험\n본문"
    msgs = analysis.build_messages(ev, rb, "v2")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == analysis.SYSTEM_PROMPT_V2 + "\n\n" + rb
    assert analysis.build_messages(ev, "", "v2")[0]["content"] == analysis.SYSTEM_PROMPT_V2
    assert msgs[1] == analysis.build_messages(ev, rb, "v1")[1]           # user는 v1과 같다
    assert msgs == variants.build_messages_variant(ev, rb, "v2")         # 채점한 eval 경로와 같다


def test_v2_shrink_uses_v2_system_length():
    ev = _evidence()
    base = len(analysis.render_evidence(ev))
    v1_len, v2_len = len(analysis.SYSTEM_PROMPT_V1), len(analysis.SYSTEM_PROMPT_V2)
    assert v2_len > v1_len
    # v1 합계는 예산 안, v2 합계는 예산 밖이 되는 런북 길이
    rb_len = analysis.PROMPT_TOKEN_BUDGET - base - v1_len - 2
    rb = "가" * rb_len
    v1, v2 = analysis.build_messages(ev, rb, "v1"), analysis.build_messages(ev, rb, "v2")
    assert v1[1]["content"] == analysis.render_evidence(ev)                      # v1: 축소 없음
    assert v2[1]["content"] == analysis.render_evidence(analysis._shrunk(ev))    # v2: 축소
    assert v2 == variants.build_messages_variant(ev, rb, "v2")


# ---------------------------------------------------------------- 2) config

@pytest.fixture
def load_config(monkeypatch):
    def _load(**env):
        for k in ("LLM_PROMPT_VERSION", "ANALYSIS_WEBHOOK_URL"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(config)
    yield _load
    monkeypatch.undo()
    importlib.reload(config)


def test_config_defaults(load_config):
    cfg = load_config().Config()
    assert cfg.llm_prompt_version == "v1" and cfg.analysis_webhook_url == ""


def test_config_env(load_config):
    cfg = load_config(LLM_PROMPT_VERSION="v2", ANALYSIS_WEBHOOK_URL=SECRET).Config()
    assert cfg.llm_prompt_version == "v2" and cfg.analysis_webhook_url == SECRET


@pytest.mark.parametrize("bad", ["v3", "V2", "", "v1 "])
def test_config_rejects_bad_version(load_config, bad):
    with pytest.raises(ValueError, match="LLM_PROMPT_VERSION"):
        load_config(LLM_PROMPT_VERSION=bad).Config()


@pytest.mark.parametrize("bad", ["http://SECRET-MARKER.example/x", "SECRET-MARKER.example/x", " https://SECRET-MARKER"])
def test_config_rejects_non_https_without_leaking(load_config, bad):
    with pytest.raises(ValueError) as e:
        load_config(ANALYSIS_WEBHOOK_URL=bad).Config()
    assert "ANALYSIS_WEBHOOK_URL" in str(e.value)
    assert "SECRET-MARKER" not in str(e.value) and "SECRET-MARKER" not in repr(e.value)


def test_config_repr_hides_analysis_url():
    cfg = Config(analysis_webhook_url=SECRET)
    assert "SECRET-MARKER" not in repr(cfg) and "SECRET-MARKER" not in str(cfg)
    assert "analysis_webhook_url" not in repr(cfg)


# ---------------------------------------------------------------- 3) 종류별 라우팅

def test_routing_with_analysis_webhook(monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with FakeWebhook("/main-SECRET-path") as main_hook, FakeWebhook("/analysis-SECRET-path") as an_hook:
        n = Notifier("slack", main_hook.url, watcher_main.emit, llm_enabled=True,
                     analysis_webhook_url=an_hook.url)
        _send_all(n)
    assert main_hook.kinds() == ["FIRING", "UPDATE", "RESOLVED"]
    assert an_hook.kinds() == ["ANALYSIS", "ANALYSIS_FAILED"]
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines()]
    assert [(e["event"], e["kind"], e["channel"]) for e in events] == [
        ("notified", "FIRING", "main"), ("notified", "UPDATE", "main"), ("notified", "RESOLVED", "main"),
        ("notified", "ANALYSIS", "analysis"), ("notified", "ANALYSIS_FAILED", "analysis")]
    assert "SECRET" not in out and "127.0.0.1" not in out


def test_routing_without_analysis_webhook_goes_to_main(monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with FakeWebhook("/main-SECRET-path") as main_hook, FakeWebhook("/analysis-SECRET-path") as an_hook:
        n = Notifier("slack", main_hook.url, watcher_main.emit, llm_enabled=True)
        _send_all(n)
    assert main_hook.kinds() == list(ALL_KINDS) and an_hook.bodies == []
    out = capsys.readouterr().out
    assert {json.loads(line)["channel"] for line in out.splitlines()} == {"main"}
    assert "SECRET" not in out and "127.0.0.1" not in out


def test_analysis_only_webhook_main_stays_dryrun(monkeypatch, capsys):
    """기존 주소가 비면 FIRING 등은 지금처럼 dryrun, 분석만 별도 주소로 나간다."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    with FakeWebhook("/analysis-SECRET-path") as an_hook:
        _send_all(Notifier("slack", "", watcher_main.emit, llm_enabled=True, analysis_webhook_url=an_hook.url))
    assert an_hook.kinds() == ["ANALYSIS", "ANALYSIS_FAILED"]
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines()]
    expected = [("notify_dryrun", "main")] * 3 + [("notified", "analysis")] * 2
    assert [(e["event"], e["channel"]) for e in events] == expected
    assert "SECRET" not in out and "127.0.0.1" not in out


def test_failure_record_does_not_leak_analysis_url(monkeypatch, capsys):
    """닫힌 포트로 보내 실패시킨다. 실패 기록(attempts.reason)에도 주소가 남지 않는다."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    with FakeWebhook("/analysis-SECRET-path") as an_hook:
        dead = an_hook.url
    n = Notifier("slack", "", watcher_main.emit, analysis_webhook_url=dead)
    assert n.send("ANALYSIS", _inc(), text="x") is False
    out = capsys.readouterr().out
    ev = json.loads(out.splitlines()[-1])
    assert ev["event"] == "notify_failed" and ev["channel"] == "analysis" and len(ev["attempts"]) == 3
    assert "SECRET" not in out


# ---------------------------------------------------------------- 4) FIRING 설명 줄

@pytest.mark.parametrize("llm_enabled,analysis_url,expected", [
    (True, "", LLM_PENDING),              # 같은 채널: 예고 줄 있음(지금과 같음)
    (True, "x", ""),                      # 별도 채널: 없음
    (False, "", ""),                      # LLM 꺼짐: 지금처럼 없음
    (False, "x", ""),
])
def test_firing_pending_line(llm_enabled, analysis_url, expected):
    events = td.Events()
    url = "https://analysis.invalid/hook-path" if analysis_url else ""
    Notifier("slack", "", events, llm_enabled=llm_enabled, analysis_webhook_url=url).send("FIRING", _inc())
    payload = events.of("notify_dryrun")[0]["payload"]
    assert _description(payload) == expected


def test_default_firing_payload_unchanged():
    """analysis 주소를 안 넘긴 Notifier는 작업 전과 같은 payload를 만든다(이벤트에는 channel만 더해짐)."""
    a, b = td.Events(), td.Events()
    Notifier("slack", "", a, llm_enabled=True).send("FIRING", _inc())
    Notifier("slack", "", b, llm_enabled=True, analysis_webhook_url="").send("FIRING", _inc())
    pa, pb = a.of("notify_dryrun")[0], b.of("notify_dryrun")[0]
    assert pa == pb and _description(pa["payload"]) == LLM_PENDING
    assert set(pa) == {"event", "provider", "kind", "incident_id", "payload", "channel"}


# ---------------------------------------------------------------- 5) dispatcher 기록

@pytest.mark.parametrize("version", ["v1", "v2"])
def test_dispatcher_trace_prompt_version(tmp_path, good_answer, version):
    events = td.Events()
    with FakeOllama("ok") as fake:
        d = td._make(tmp_path, events, url=fake.url, llm_prompt_version=version)
        d.submit("FIRING", _inc(1), evidence=td._ev(1))
        assert td._wait(lambda: (tmp_path / "llm_runs.jsonl").exists())
        body = td._analysis_requests(fake)[0]
        d.close()
    rec = json.loads((tmp_path / "llm_runs.jsonl").read_text())
    assert rec["environment"]["prompt_version"] == version
    prompt = {s["id"]: s for s in rec["stages"]}["prompt"]["output"]
    assert prompt["version"] == version
    expected_system = analysis.build_messages(td._ev(1), d.runbooks_text, version)[0]["content"]
    assert body["messages"][0]["content"] == expected_system
    assert prompt["system_sha256"] == hashlib.sha256(expected_system.encode("utf-8")).hexdigest()


@pytest.fixture
def good_answer(monkeypatch):
    import fake_ollama
    msg = {"role": "assistant", "content": json.dumps(td.GOOD_ANSWER, ensure_ascii=False)}
    monkeypatch.setitem(fake_ollama.OK_BODY, "message", msg)
