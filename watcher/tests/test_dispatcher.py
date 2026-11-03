"""알림·LLM 워커 스레드(dispatcher)와 새 알림 종류(ANALYSIS, ANALYSIS_FAILED).

가짜 Ollama(tests/fake_ollama.py)와 notifier의 dry-run(웹훅 없음)으로 시험한다. 네트워크 밖으로 나가지 않는다.
"""
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import fake_ollama
import pytest
from fake_ollama import FakeOllama

import analysis
import dispatcher as dispatcher_mod
import llm_trace
from config import Config
from dispatcher import FOREIGN_NOTE, Dispatcher, LlmRequest
from notifier import COLOR, LLM_PENDING, Notifier, build_message, to_slack
from state import Incident
from stats import WindowStats

GOOD_ANSWER = {"summary": "5xx가 급증했다.", "causes": ["/error 경로 오류", "DB 연결 실패"],
               "checks": ["경로별 5xx 확인", "db 상태 확인"]}


# ---------------------------------------------------------------- 도우미

class Events:
    """emit을 대신해 이벤트를 모은다(여러 스레드에서 불림)."""

    def __init__(self):
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, event, **fields):
        with self._lock:
            self.items.append({"event": event, **fields})

    def of(self, event) -> list[dict]:
        with self._lock:
            return [e for e in self.items if e["event"] == event]

    def sent(self) -> list[tuple[str, int]]:
        return [(e["kind"], e["incident_id"]) for e in self.of("notify_dryrun")]


def _wait(pred, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _cfg(tmp_path, **kw) -> Config:
    base = dict(llm_enabled=True, ollama_url="http://127.0.0.1:9", llm_connect_timeout=1.0, llm_timeout=2.0,
                llm_trace_path=str(tmp_path / "llm_runs.jsonl"))
    base.update(kw)
    return Config(**base)


def _inc(id=1, state="FIRING", rules=("R1",)) -> Incident:
    return Incident(id, "visit-counter", state, "critical", list(rules), "2026-11-02T00:00:00+00:00",
                    "2026-11-02T00:06:00+00:00" if state == "RESOLVED" else None, 1, 0)


def _ev(id=1) -> dict:
    now = datetime.fromisoformat("2026-11-02T00:01:00+00:00").timestamp()
    return analysis.build_evidence(_inc(id), WindowStats(100, 40, 30.0, 100.0), [], 300.0,
                                   {"consecutive_failures": 0, "detail": "HTTP 200"}, now, current_rules=["R1"])


def _make(tmp_path, events, url=None, notifier=None, warmup=False, **kw) -> Dispatcher:
    cfg = _cfg(tmp_path, **({"ollama_url": url} if url else {}), **kw)
    n = notifier or Notifier("slack", "", events, llm_enabled=cfg.llm_enabled)
    d = Dispatcher(cfg, n, events, analysis.load_runbooks(Path(__file__).resolve().parent.parent / "runbooks"))
    d.start(warmup=warmup)
    return d


@pytest.fixture
def good_answer(monkeypatch):
    msg = {"role": "assistant", "content": json.dumps(GOOD_ANSWER, ensure_ascii=False)}
    monkeypatch.setitem(fake_ollama.OK_BODY, "message", msg)


def _analysis_requests(fake) -> list[dict]:
    """가짜 Ollama가 받은 분석 요청(format이 있는 /api/chat)."""
    bodies = [json.loads(r["body"]) for r in fake.requests if r["path"] == "/api/chat"]
    return [b for b in bodies if "format" in b]


def _payload(events, kind, inc_id=1) -> dict:
    return next(e["payload"] for e in events.of("notify_dryrun") if e["kind"] == kind and e["incident_id"] == inc_id)


def _description(payload) -> str:
    blocks = payload["attachments"][0]["blocks"]
    return next((b["text"]["text"] for b in blocks if b["type"] == "section" and "text" in b), "")


class SlowNotifier:
    url = ""

    def __init__(self, delay):
        self.delay, self.calls = delay, []

    def send(self, kind, inc, **ctx):
        time.sleep(self.delay)
        self.calls.append((kind, inc.id))
        return True


class GateNotifier:
    """첫 send에서 gate가 열릴 때까지 막힌다. 큐를 원하는 모양으로 채우기 위해 쓴다."""
    url = ""

    def __init__(self):
        self.entered, self.gate, self.calls = threading.Event(), threading.Event(), []

    def send(self, kind, inc, **ctx):
        self.entered.set()
        self.gate.wait(10)
        self.calls.append((kind, inc.id))
        return True


# ---------------------------------------------------------------- submit은 막히지 않는다

def test_submit_is_fast_when_send_queue_full_and_notifier_slow(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher_mod, "SEND_QUEUE_SIZE", 10)
    events = Events()
    d = _make(tmp_path, events, notifier=SlowNotifier(0.5), llm_enabled=False)
    worst = 0.0
    for i in range(100):
        inc, ev, st = _inc(i), _ev(i), WindowStats(1, 0, 1.0, 1.0)      # 인자는 재기 전에 만든다
        t0 = time.perf_counter()
        d.submit("FIRING" if i % 2 else "UPDATE", inc, evidence=ev, stats=st)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.05, worst
    assert events.of("notify_dropped")                                 # 가득 차서 버린 기록이 남는다


def test_submit_is_fast_when_ollama_slow_and_llm_queue_full(tmp_path, good_answer):
    events = Events()
    with FakeOllama("slow", delay=3.0) as fake:
        d = _make(tmp_path, events, url=fake.url, llm_timeout=5.0)
        worst = 0.0
        for i in range(100):
            inc, ev = _inc(i), _ev(i)
            t0 = time.perf_counter()
            d.submit("FIRING", inc, evidence=ev)
            worst = max(worst, time.perf_counter() - t0)
        assert worst < 0.05, worst
        assert _wait(lambda: len(events.of("notify_dryrun")) == 100)   # 기본 알림은 Ollama와 무관하게 모두 나감
        skipped = [e for e in events.of("llm_skipped") if e["reason"] == "queue_full"]
        assert skipped                                                 # llm_queue(20)가 차서 건너뜀


# ---------------------------------------------------------------- 버림 정책과 순서

def test_drop_oldest_update_and_keep_firing_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher_mod, "SEND_QUEUE_SIZE", 5)
    events, n = Events(), GateNotifier()
    d = _make(tmp_path, events, notifier=n, llm_enabled=False)
    d.submit("FIRING", _inc(1))
    assert n.entered.wait(5)                                          # 워커가 F1을 잡고 막혀 있다. 큐는 비어 있음
    for kind, i in [("UPDATE", 1), ("FIRING", 2), ("UPDATE", 2), ("RESOLVED", 1), ("FIRING", 3)]:
        d.submit(kind, _inc(i))                                       # 큐 5칸이 찬다
    d.submit("RESOLVED", _inc(2))                                     # 가장 오래된 UPDATE(1)를 버림
    d.submit("FIRING", _inc(4))                                       # 남은 UPDATE(2)를 버림
    d.submit("FIRING", _inc(5))                                       # 버릴 UPDATE가 없다 → 새 항목을 버림
    n.gate.set()
    assert _wait(lambda: len(n.calls) == 6)
    assert n.calls == [("FIRING", 1), ("FIRING", 2), ("RESOLVED", 1), ("FIRING", 3), ("RESOLVED", 2), ("FIRING", 4)]
    dropped = [(e["kind"], e["incident_id"], e["reason"]) for e in events.of("notify_dropped")]
    assert dropped == [("UPDATE", 1, "oldest_update"), ("UPDATE", 2, "oldest_update"), ("FIRING", 5, "queue_full")]


def test_notifications_keep_submit_order(tmp_path):
    events = Events()
    d = _make(tmp_path, events, llm_enabled=False)
    order = [("FIRING", 1), ("UPDATE", 1), ("FIRING", 2), ("RESOLVED", 1), ("UPDATE", 2), ("RESOLVED", 2)]
    for kind, i in order:
        d.submit(kind, _inc(i, state="RESOLVED" if kind == "RESOLVED" else "FIRING"),
                 **({"added": ["R2"]} if kind == "UPDATE" else {}))
    assert _wait(lambda: len(events.sent()) == 6)
    assert events.sent() == order


def test_submit_copies_incident(tmp_path):
    events, n = Events(), GateNotifier()
    d = _make(tmp_path, events, notifier=n, llm_enabled=False)
    d.submit("FIRING", _inc(9))
    assert n.entered.wait(5)
    inc = _inc(10)
    d.submit("FIRING", inc)
    inc.id = 999                                                       # 메인 스레드가 나중에 바꿔도
    n.gate.set()
    assert _wait(lambda: len(n.calls) == 2)
    assert n.calls[1] == ("FIRING", 10)                                # 큐에 넣은 시점의 값이 나간다


# ---------------------------------------------------------------- 분석: FIRING에만, 장애당 한 번

def test_analysis_only_for_firing_and_after_it(tmp_path, good_answer):
    events = Events()
    with FakeOllama("ok") as fake:
        d = _make(tmp_path, events, url=fake.url)
        d.submit("UPDATE", _inc(2), evidence=_ev(2), added=["R2"])
        d.submit("RESOLVED", _inc(3, state="RESOLVED"), evidence=_ev(3))
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        assert _wait(lambda: ("ANALYSIS", 1) in events.sent())
        time.sleep(0.2)
        assert len(_analysis_requests(fake)) == 1
    sent = events.sent()
    assert sent.index(("FIRING", 1)) < sent.index(("ANALYSIS", 1))
    assert [k for k, _ in sent].count("ANALYSIS") == 1
    payload = _payload(events, "ANALYSIS")
    assert payload["text"] == "[ANALYSIS] CRITICAL · visit-counter"
    assert payload["attachments"][0]["color"] == COLOR["ANALYSIS"]
    desc = _description(payload)
    assert desc.startswith("5xx가 급증했다.") and "원인 후보" in desc and "AI 생성, 검증 전" in desc
    assert [e["event"] for e in events.items if e["event"].startswith("llm_")] == ["llm_start", "llm_done"]


def test_same_incident_is_analyzed_once(tmp_path, good_answer):
    events = Events()
    with FakeOllama("ok") as fake:
        d = _make(tmp_path, events, url=fake.url)
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        assert _wait(lambda: events.of("llm_done") and len(events.sent()) == 3)
        time.sleep(0.2)
        assert len(_analysis_requests(fake)) == 1
    assert [e["reason"] for e in events.of("llm_skipped")] == ["already_analyzed"]


def test_seen_ids_are_bounded():
    seen = dispatcher_mod._Seen(limit=3)
    for i in range(5):
        assert seen.add(i)
    assert len(seen) == 3 and 0 not in seen and 4 in seen
    assert not seen.add(4)


@pytest.mark.parametrize("mode,reason", [("drop", "connect_error"), ("slow", "timeout"),
                                         ("truncated", "truncated"), ("ok", "bad_output")])
def test_analysis_failure_sends_one_failed_notice(tmp_path, mode, reason):
    events = Events()                                                  # ok 모드의 기본 답은 checks가 없어 bad_output
    with FakeOllama(mode, delay=3.0) as fake:
        d = _make(tmp_path, events, url=fake.url, llm_timeout=0.5)
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        assert _wait(lambda: ("ANALYSIS_FAILED", 1) in events.sent())
        d._analyze(LlmRequest(1, "window", _ev(1)))                    # 같은 장애가 또 실패해도
        trace = tmp_path / "llm_runs.jsonl"
        assert _wait(lambda: trace.exists() and len(trace.read_text().splitlines()) == 2)
    assert events.sent() == [("FIRING", 1), ("ANALYSIS_FAILED", 1)]    # 실패 알림은 1번, 기본 알림은 그대로
    assert [e["reason"] for e in events.of("llm_failed")] == [reason, reason]
    payload = _payload(events, "ANALYSIS_FAILED")
    assert _description(payload) == f"AI 분석 실패({reason})"
    assert payload["attachments"][0]["color"] == COLOR["ANALYSIS_FAILED"]
    recs = [json.loads(line) for line in (tmp_path / "llm_runs.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and all(r["status"] == "error" and r["error"] == reason for r in recs)
    assert all(llm_trace.validate_record(r) == [] for r in recs)


def test_analysis_after_resolve_is_marked(tmp_path, good_answer):
    events = Events()
    with FakeOllama("slow", delay=0.5) as fake:
        d = _make(tmp_path, events, url=fake.url)
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        d.submit("RESOLVED", _inc(1, state="RESOLVED"))
        assert _wait(lambda: ("ANALYSIS", 1) in events.sent())
    assert events.sent() == [("FIRING", 1), ("RESOLVED", 1), ("ANALYSIS", 1)]
    assert _payload(events, "ANALYSIS")["text"].endswith(" (복구 후 도착)")


def test_analysis_text_goes_through_mask_and_gets_flag_line(tmp_path, monkeypatch):
    answer = dict(GOOD_ANSWER, summary="원인은 数据库 문제다.")
    monkeypatch.setitem(fake_ollama.OK_BODY, "message", {"role": "assistant",
                                                         "content": json.dumps(answer, ensure_ascii=False)})
    masked = []
    monkeypatch.setattr(analysis, "mask", lambda t: masked.append(t) or t)
    events = Events()
    with FakeOllama("ok") as fake:
        d = _make(tmp_path, events, url=fake.url)
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        assert _wait(lambda: ("ANALYSIS", 1) in events.sent())
    desc = _description(_payload(events, "ANALYSIS"))
    assert desc.splitlines()[-1] == FOREIGN_NOTE
    assert len(masked) == 2 and masked[1] == desc                      # user 글 1번 + 알림 본문 1번


# ---------------------------------------------------------------- 옵션 7개와 실행 기록

def test_seven_options_in_request_and_trace(tmp_path, good_answer):
    events = Events()
    with FakeOllama("ok") as fake:
        d = _make(tmp_path, events, url=fake.url, llm_seed=7, llm_top_k=11)
        d.submit("FIRING", _inc(1), evidence=_ev(1))
        assert _wait(lambda: (tmp_path / "llm_runs.jsonl").exists())
        body = _analysis_requests(fake)[0]
    expected = {"temperature": 0.0, "seed": 7, "top_k": 11, "top_p": 0.9, "repeat_penalty": 1.0,
                "num_predict": 512, "num_ctx": 4096}
    assert body["options"] == expected
    assert body["format"] == analysis.ANALYSIS_SCHEMA and body["keep_alive"] == "24h"
    rec = json.loads((tmp_path / "llm_runs.jsonl").read_text())
    assert llm_trace.validate_record(rec) == []
    assert rec["settings"] == expected and rec["status"] == "ok" and rec["incident"] == {"id": 1, "trigger": "window"}
    stages = {s["id"]: s for s in rec["stages"]}
    assert list(stages) == ["evidence", "prompt", "llm_call", "parse", "notify"]
    assert stages["llm_call"]["output"]["options"] == expected
    assert stages["evidence"]["output"] == _ev(1)
    prompt = stages["prompt"]["output"]
    assert prompt["version"] == "v1" and prompt["user"].startswith("[장애 요약]") and len(prompt["system_sha256"]) == 64
    assert stages["parse"]["output"] == {"ok": True, "reason": None, "flags": []}
    assert stages["notify"]["output"] == {"sent": True, "kind": "ANALYSIS", "mode": "dryrun"}
    assert prompt["user"] not in json.dumps(events.items, ensure_ascii=False)     # 프롬프트 본문은 stdout에 없다


# ---------------------------------------------------------------- 워커 감시

class ExitOnceNotifier:
    url = ""

    def __init__(self, exc):
        self.exc, self.calls = exc, []

    def send(self, kind, inc, **ctx):
        if not self.calls:
            self.calls.append("boom")
            raise self.exc
        self.calls.append((kind, inc.id))
        return True


def test_worker_error_is_logged_and_loop_continues(tmp_path):
    events, n = Events(), ExitOnceNotifier(RuntimeError("x"))
    d = _make(tmp_path, events, notifier=n, llm_enabled=False)
    d.submit("FIRING", _inc(1))
    d.submit("FIRING", _inc(2))
    assert _wait(lambda: len(n.calls) == 2)
    assert [e["worker"] for e in events.of("worker_error")] == ["notify"]
    assert d.threads["notify"].is_alive()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")   # 일부러 스레드를 죽인다
def test_ensure_alive_restarts_dead_notify_worker(tmp_path):
    events, n = Events(), ExitOnceNotifier(SystemExit())     # Exception이 아니어서 루프 밖으로 빠져 스레드가 죽는다
    d = _make(tmp_path, events, notifier=n, llm_enabled=False)
    d.submit("FIRING", _inc(1))
    assert _wait(lambda: not d.threads["notify"].is_alive())
    d.ensure_alive()
    assert [e["worker"] for e in events.of("worker_restarted")] == ["notify"]
    d.submit("FIRING", _inc(2))
    assert _wait(lambda: ("FIRING", 2) in n.calls)
    d.ensure_alive()                                                   # 살아 있으면 아무것도 안 한다
    assert len(events.of("worker_restarted")) == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_ensure_alive_restarts_dead_llm_worker(tmp_path):
    events = Events()
    cfg = _cfg(tmp_path)
    calls = []

    def chat(*a, **kw):
        calls.append(1)
        raise SystemExit
    d = Dispatcher(cfg, Notifier("slack", "", events, llm_enabled=True), events, chat=chat)
    d.start(warmup=False)
    d.submit("FIRING", _inc(1), evidence=_ev(1))
    assert _wait(lambda: not d.threads["llm"].is_alive())
    d.ensure_alive()
    assert [e["worker"] for e in events.of("worker_restarted")] == ["llm"]
    d.submit("FIRING", _inc(2), evidence=_ev(2))
    assert _wait(lambda: len(calls) == 2)


# ---------------------------------------------------------------- 설정

def test_llm_disabled_has_no_llm_thread_and_notifies(tmp_path):
    events = Events()
    d = _make(tmp_path, events, llm_enabled=False, warmup=True)
    assert d.llm_queue is None and set(d.threads) == {"notify"} and d.warmup_thread is None
    d.submit("FIRING", _inc(1), evidence=_ev(1))
    assert _wait(lambda: events.sent() == [("FIRING", 1)])
    time.sleep(0.1)
    assert not [e for e in events.items if e["event"].startswith(("llm_", "warmup_"))]
    assert _description(_payload(events, "FIRING")) == ""             # 자리표시 줄이 없다


def test_hold_sec_above_zero_refuses_to_start(tmp_path):
    with pytest.raises(ValueError, match=r"LLM_HOLD_SEC>0은 아직 지원하지 않는다"):
        Dispatcher(_cfg(tmp_path, llm_hold_sec=1.5), Notifier("slack", "", Events()), Events())


# ---------------------------------------------------------------- 예열

@pytest.mark.parametrize("mode,event", [("ok", "warmup_done"), ("truncated", "warmup_done"),
                                        ("drop", "warmup_failed")])
def test_warmup(tmp_path, mode, event):
    events = Events()
    with FakeOllama(mode) as fake:
        d = _make(tmp_path, events, url=fake.url, warmup=True)
        d.warmup_thread.join(10)
        bodies = [json.loads(r["body"]) for r in fake.requests if r["path"] == "/api/chat"]
    assert [e["event"] for e in events.items if e["event"].startswith("warmup_")] == [event]
    assert len(bodies) == 1 and "format" not in bodies[0]
    assert bodies[0]["options"]["num_predict"] == 1 and len(bodies[0]["options"]) == 7
    if event == "warmup_failed":
        assert events.of("warmup_failed")[0]["reason"] == "connect_error"
    else:
        assert d.ollama_version == "0.40.2"


# ---------------------------------------------------------------- notifier 새 종류와 FIRING 설명

def test_firing_description_depends_on_llm_enabled():
    on = build_message("FIRING", _inc(), llm_enabled=True)
    off = build_message("FIRING", _inc())
    assert on.description == LLM_PENDING == "AI 분석을 생성 중입니다. 완료되면 이어서 보냅니다."
    assert off.description == ""
    blocks = to_slack(off)["attachments"][0]["blocks"]
    assert [b["type"] for b in blocks] == ["section", "context"] and "fields" in blocks[0]


def test_analysis_messages_colors_and_late_suffix():
    ref = dispatcher_mod.IncidentRef(3, "visit-counter", "warning")
    m = build_message("ANALYSIS", ref, text="본문", late=True)
    assert m.title == "[ANALYSIS] WARNING · visit-counter (복구 후 도착)" and m.description == "본문"
    slack = to_slack(m)
    assert slack["attachments"][0]["color"] == COLOR["ANALYSIS"] == "#3B82F6"
    assert [b["type"] for b in slack["attachments"][0]["blocks"]] == ["section", "context"]   # 빈 fields 블록 없음
    f = build_message("ANALYSIS_FAILED", ref, text="AI 분석 실패(timeout)")
    assert f.title == "[ANALYSIS_FAILED] WARNING · visit-counter" and COLOR["ANALYSIS_FAILED"] == "#8B8D98"


# ---------------------------------------------------------------- main.make_evidence

def test_make_evidence_off_and_on(tmp_path):
    import main
    inc = _inc(1, rules=("R4",))
    now = datetime.now(UTC).timestamp()
    assert main.make_evidence(_cfg(tmp_path, llm_enabled=False), inc, WindowStats(0, 0, 0.0, 0.0), None,
                              None, {"consecutive_failures": 2, "detail": "timeout"}, now) is None
    ev = main.make_evidence(_cfg(tmp_path), inc, WindowStats(0, 0, 0.0, 0.0), None, None,
                            {"consecutive_failures": 2, "detail": "timeout"}, now, current_rules=["R4"])
    assert ev["records_available"] is False and ev["incident"]["fired_rules"] == [{"rule": "R4",
                                                                                   "detail": "연속 실패 2회"}]
