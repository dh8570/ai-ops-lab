"""A1-T4: scripts/acceptance의 가짜 Ollama 서버와 LLM 알림 합격 시험(F·G·H·R4) 판정 함수.

가짜 서버는 로컬 임시 포트로 실제로 띄워 watcher의 llm_client·analysis로 읽어 본다.
판정 함수는 로그 이벤트 목록(합성)만 넣어 시험한다. docker는 부르지 않는다.
"""
import atexit
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import analysis
import llm_client


def _load_acceptance() -> Path:
    """scripts/acceptance의 세 파일을 임시 폴더의 레포 모양 경로(.../scripts/acceptance)로 복사해 import 경로에 둔다.
    alerts.py가 ROOT = parents[2]로 레포 루트를 구하는데, check-watcher.sh 컨테이너는 이 폴더를 /acceptance에
    마운트해 그 계산이 깨지기 때문이다. 복사본이라 내용은 같다."""
    here = Path(__file__).resolve()
    src = next((c for c in (here.parents[2] / "scripts" / "acceptance", Path("/acceptance"))   # 레포 / 컨테이너
                if (c / "llm_alerts.py").exists()), None)
    if src is None:
        raise RuntimeError("scripts/acceptance를 찾지 못했다")
    root = Path(tempfile.mkdtemp(prefix="acceptance-llm-test-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    dst = root / "repo" / "scripts" / "acceptance"
    dst.mkdir(parents=True)
    for name in ("alerts.py", "llm_alerts.py", "fake_ollama_server.py"):
        shutil.copy(src / name, dst / name)
    return dst


sys.path.insert(0, str(_load_acceptance()))
la = importlib.import_module("llm_alerts")
fake = importlib.import_module("fake_ollama_server")

OPTIONS = llm_client.LlmOptions(temperature=0, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0,
                                num_predict=512, num_ctx=4096)
MESSAGES = [{"role": "system", "content": "시스템"}, {"role": "user", "content": "사용자"}]


# ---------------------------------------------------------------- 가짜 Ollama 서버

@pytest.fixture
def server():
    srv = fake.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.state.stop.set()
    srv.shutdown()
    srv.server_close()


def _http(srv, method, path, obj=None):
    data = None if obj is None else json.dumps(obj).encode()
    req = urllib.request.Request(srv.url + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _chat(srv, timeout=5.0):
    return llm_client.chat(srv.url, "qwen2.5:7b", MESSAGES, OPTIONS, fmt=analysis.ANALYSIS_SCHEMA,
                           connect_timeout=2.0, total_timeout=timeout)


def test_fake_analysis_answer_passes_watcher_parsing(server):
    res = _chat(server)
    assert res.ok and res.done_reason == "stop"
    body = json.loads(res.text)
    assert (len(body["causes"]), len(body["checks"])) == (2, 2)
    parsed = analysis.parse_answer(res.text)
    assert parsed.ok and parsed.flags == []              # 스키마를 만족하고 한국어만 쓴다(foreign_script 없음)
    for name in ("load_ms", "prompt_eval_count", "prompt_eval_cached_count", "prompt_eval_ms",
                 "eval_count", "eval_ms", "total_ms"):
        assert getattr(res, name) is not None, name


def test_fake_version(server):
    assert _http(server, "GET", "/api/version") == (200, '{"version": "fake"}')
    assert llm_client.version(server.url) == "fake"


def test_fake_mode_endpoint_counts_chat_requests_and_last_body(server):
    assert _http(server, "GET", "/__last")[0] == 404
    assert json.loads(_http(server, "GET", "/__mode")[1]) == {"mode": "analysis", "delay": 0.0, "chat_requests": 0}
    _chat(server)
    _chat(server)
    assert json.loads(_http(server, "GET", "/__mode")[1])["chat_requests"] == 2
    status, last = _http(server, "GET", "/__last")
    assert status == 200 and json.loads(last)["messages"] == MESSAGES


def test_fake_mode_endpoint_validates_input(server):
    assert _http(server, "POST", "/__mode", {"mode": "nope"})[0] == 400
    assert _http(server, "POST", "/__mode", {"mode": "slow", "delay": -1})[0] == 400
    assert _http(server, "POST", "/__mode", {"mode": "slow", "delay": "3"})[0] == 400
    assert _http(server, "POST", "/__mode", {"delay": 3})[0] == 400
    status, body = _http(server, "POST", "/__mode", {"mode": "slow", "delay": 7})
    assert status == 200 and json.loads(body) == {"mode": "slow", "delay": 7.0, "chat_requests": 0}
    # delay를 생략하면 이전 값이 그대로 남는다
    assert json.loads(_http(server, "POST", "/__mode", {"mode": "analysis"})[1])["delay"] == 7.0


@pytest.mark.parametrize("mode,reason", [("drop", "connect_error"), ("http500", "http_500"), ("garbage", "bad_json")])
def test_fake_failure_modes(server, mode, reason):
    _http(server, "POST", "/__mode", {"mode": mode})
    res = _chat(server)
    assert (res.ok, res.reason) == (False, reason)


def test_fake_slow_mode_waits_then_answers(server):
    _http(server, "POST", "/__mode", {"mode": "slow", "delay": 0.4})
    t0 = time.monotonic()
    res = _chat(server)
    assert res.ok and time.monotonic() - t0 >= 0.4
    _http(server, "POST", "/__mode", {"mode": "slow", "delay": 5})
    assert _chat(server, timeout=0.5).reason == "timeout"            # watcher의 총 상한이 지연보다 먼저 닿는다


def test_fake_client_snippet_talks_to_the_server(server):
    """compose exec로 부르는 코드(FAKE_CLIENT)가 실제 서버와 맞는지. 포트만 시험 서버 것으로 바꿔 실행한다."""
    code = la.FAKE_CLIENT.replace("11434", str(server.server_address[1]))
    run = lambda *a: subprocess.run([sys.executable, "-I", "-c", code, *a],  # noqa: E731
                                    capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(run("set", "slow", "90").stdout) == {"mode": "slow", "delay": 90.0, "chat_requests": 0}
    assert json.loads(run("set", "analysis", "").stdout)["mode"] == "analysis"
    assert json.loads(run("get").stdout)["delay"] == 90.0


# ---------------------------------------------------------------- 합성 로그 도우미

T0 = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)


def ev(sec, event, **kw):
    return {"event": event, "_dt": T0 + timedelta(seconds=sec), **kw}


def notify(sec, kind, inc=1):
    return ev(sec, "notify_dryrun", kind=kind, incident_id=inc)


def windows(first, last, step=60):
    return [ev(s, "window") for s in range(first, last + 1, step)]


def passed(rows):
    return {item: ok for item, ok, _ in rows}


def record(inc=1, status="ok", sha="a" * 64, settings=None, user="[장애 요약]\n..."):
    settings = {k: 1 for k in la.OPTION_KEYS} if settings is None else settings
    return {"incident": {"id": inc, "trigger": "window"}, "status": status, "settings": settings,
            "stages": [{"id": "evidence", "output": {}},
                       {"id": "prompt", "output": {"system_sha256": sha, "user": user}}]}


# ---------------------------------------------------------------- llm_view, 창 간격

def test_llm_view_collects_one_incident_only_and_keeps_order():
    events = [notify(0, "FIRING"), notify(1, "FIRING", inc=2), ev(2, "llm_start", incident_id=1),
              ev(3, "llm_failed", incident_id=2, reason="timeout"), notify(4, "ANALYSIS"),
              ev(5, "worker_error", worker="llm", error="X")]
    v = la.llm_view(events, 1)
    assert [len(v["notify"][k]) for k in ("FIRING", "ANALYSIS")] == [1, 1]
    assert len(v["llm_start"]) == 1 and v["llm_failed"] == []
    assert v["pos"]["FIRING"] == [0] and v["pos"]["ANALYSIS"] == [4]
    assert len(v["worker_error"]) == 1                    # worker_error는 장애 id와 무관하게 모두 센다
    assert la.llm_view(events, None)["notify"]["FIRING"] == []


def test_max_window_gap():
    start, end = T0 + timedelta(seconds=100), T0 + timedelta(seconds=400)
    evs = windows(60, 400)                                # 60, 120, ... 360 (400은 창이 아님)
    assert la.max_window_gap(evs, start, end) == 60.0     # 직전 창(60) → 120 ... 360 → 끝(400)은 40
    gap = la.max_window_gap(windows(60, 120) + [ev(210, "window")] + windows(270, 360), start, end)
    assert gap == 90.0                                    # 120 → 210
    assert la.max_window_gap([ev(10, "probe")], start, end) is None
    assert la.max_window_gap(windows(60, 120), start, end) == 280.0    # 시작 뒤 창 하나, 마지막 창에서 끝까지 비어 있음


# ---------------------------------------------------------------- F

def _f_events(**over):
    events = [notify(100, "FIRING"), ev(101, "llm_start", incident_id=1),
              ev(101, "llm_failed", incident_id=1, reason="connect_error", wall_ms=3.0), notify(102, "ANALYSIS_FAILED")]
    return events + over.get("extra", [])


def test_f_passes_when_ollama_is_down():
    rows = la.rows_f(la.llm_view(_f_events(), 1))
    assert all(passed(rows).values()) and list(passed(rows)) == ["F1", "F2", "F3", "F4"]


@pytest.mark.parametrize("extra,failed", [
    ([ev(103, "llm_failed", incident_id=1, reason="connect_error")], "F1"),             # 실패 이벤트가 두 건
    ([notify(103, "ANALYSIS")], "F3"),                                                  # 분석 알림이 옴
    ([ev(103, "llm_done", incident_id=1)], "F3"),
    ([ev(103, "worker_error", worker="notify", error="X")], "F4"),
])
def test_f_fails(extra, failed):
    assert passed(la.rows_f(la.llm_view(_f_events(extra=extra), 1)))[failed] is False


def test_f_fails_on_wrong_reason_missing_notice_and_wrong_order():
    wrong = [notify(100, "FIRING"), ev(101, "llm_failed", incident_id=1, reason="timeout"),
             notify(102, "ANALYSIS_FAILED")]
    assert passed(la.rows_f(la.llm_view(wrong, 1)))["F1"] is False
    none = [notify(100, "FIRING"), ev(101, "llm_failed", incident_id=1, reason="connect_error")]
    assert passed(la.rows_f(la.llm_view(none, 1)))["F2"] is False
    early = [notify(99, "ANALYSIS_FAILED"), notify(100, "FIRING"),
             ev(101, "llm_failed", incident_id=1, reason="connect_error")]
    assert passed(la.rows_f(la.llm_view(early, 1)))["F2"] is False                      # FIRING보다 먼저 옴


# ---------------------------------------------------------------- G

def _g_events(gap=30, wall=30012.0, reason="timeout"):
    return [notify(100, "FIRING"), ev(100, "llm_start", incident_id=1),
            ev(100 + gap, "llm_failed", incident_id=1, reason=reason, wall_ms=wall),
            notify(100 + gap, "ANALYSIS_FAILED")]


def test_g_passes_when_loop_keeps_running_and_timeout_is_near_30s():
    v = la.llm_view(_g_events(), 1)
    assert all(passed(la.rows_g(v, 60.0, 60)).values())
    assert passed(la.rows_g(v, 75.0, 60))["G1"] is True                                 # 경계: window_sec + 15


@pytest.mark.parametrize("kwargs,item", [
    (dict(gap=10, wall=10000.0), "G2"),             # 너무 일찍(타임아웃이 아니라 다른 이유로 끊긴 모양)
    (dict(gap=45, wall=45000.0), "G2"),             # 너무 늦게
    (dict(wall=None), "G2"),                        # wall_ms 없음
    (dict(wall=12000.0), "G2"),                     # 로그 시각은 맞는데 wall_ms가 범위 밖
    (dict(reason="connect_error"), "G2"),
])
def test_g_timeout_checks_fail(kwargs, item):
    assert passed(la.rows_g(la.llm_view(_g_events(**kwargs), 1), 60.0, 60))[item] is False


def test_g_fails_when_loop_stalls_or_notices_are_wrong():
    v = la.llm_view(_g_events(), 1)
    assert passed(la.rows_g(v, 90.0, 60))["G1"] is False                                # 30초 멈춘 감시 루프
    assert passed(la.rows_g(v, None, 60))["G1"] is False                                # 측정 못 함
    no_firing = la.llm_view(_g_events()[1:], 1)
    assert passed(la.rows_g(no_firing, 60.0, 60))["G2"] is False
    wrong = la.llm_view(_g_events() + [notify(131, "ANALYSIS")], 1)
    assert passed(la.rows_g(wrong, 60.0, 60))["G3"] is False


# ---------------------------------------------------------------- H

def _h_events(extra=()):
    return [notify(100, "FIRING"), ev(100, "llm_start", incident_id=1),
            ev(103, "llm_done", incident_id=1, wall_ms=2500), notify(103, "ANALYSIS"), *extra]


def test_h_passes_for_one_analysis_after_firing_with_a_good_record():
    rows = la.rows_h(la.llm_view(_h_events(), 1), [record()])
    assert all(passed(rows).values()) and list(passed(rows)) == ["H1", "H2", "H3"]


def test_h_fails_on_duplicate_early_or_failed_analysis():
    dup = la.llm_view(_h_events([notify(104, "ANALYSIS")]), 1)
    assert passed(la.rows_h(dup, [record()]))["H1"] is False
    early = la.llm_view([notify(99, "ANALYSIS"), *_h_events()], 1)
    assert passed(la.rows_h(early, [record()]))["H1"] is False                          # FIRING보다 먼저
    failed = la.llm_view(_h_events([ev(104, "llm_failed", incident_id=1, reason="timeout")]), 1)
    assert passed(la.rows_h(failed, [record()]))["H2"] is False
    notice = la.llm_view(_h_events([notify(105, "ANALYSIS_FAILED")]), 1)
    assert passed(la.rows_h(notice, [record()]))["H2"] is False


@pytest.mark.parametrize("records", [
    [],                                                                  # 기록 없음
    [record(), record()],                                                # 두 줄
    [record(status="error")],
    [record(settings={k: 1 for k in la.OPTION_KEYS if k != "seed"})],    # 옵션 하나 빠짐
    [record(settings={**{k: 1 for k in la.OPTION_KEYS}, "top_k": None})],
])
def test_h_record_checks_fail(records):
    assert passed(la.rows_h(la.llm_view(_h_events(), 1), records))["H3"] is False


def test_h_system_hash_must_match_across_incidents():
    assert passed(la.rows_h_hash(["a" * 64, "a" * 64]))["H4"] is True
    assert passed(la.rows_h_hash(["a" * 64, "b" * 64]))["H4"] is False
    assert passed(la.rows_h_hash(["a" * 64]))["H4"] is False                            # 비교할 둘째 장애 없음
    assert passed(la.rows_h_hash([None, None]))["H4"] is False                          # 기록을 못 읽음


# ---------------------------------------------------------------- R4

R4_USER = "[장애 요약]\n장애 #3\n[참고] 이 장애는 로그 창이 닫히기 전에 열려 오류 유형·경로 집계가 없음"


def test_r4_passes_with_one_analysis_and_no_window_line():
    v = la.llm_view(_h_events(), 1)
    rows = la.rows_r4(v, [record(user=R4_USER)])
    assert all(passed(rows).values()) and list(passed(rows)) == ["S1", "S2", "S3"]
    failed_only = la.llm_view([notify(100, "FIRING"), ev(100, "llm_start", incident_id=1),
                               notify(131, "ANALYSIS_FAILED")], 1)
    assert passed(la.rows_r4(failed_only, [record(user=R4_USER)]))["S1"] is True       # 실패 알림 1건도 '최대 1건'


def test_r4_fails():
    v = la.llm_view(_h_events(), 1)
    assert passed(la.rows_r4(v, [record(user="[최근 60초] 요청 0\n" + R4_USER)]))["S3"] is False
    assert passed(la.rows_r4(v, [record(user="[장애 요약]")]))["S3"] is False           # [참고] 줄이 없음
    assert passed(la.rows_r4(v, []))["S2"] is False and passed(la.rows_r4(v, []))["S3"] is False
    two = la.llm_view(_h_events([notify(110, "ANALYSIS_FAILED")]), 1)
    assert passed(la.rows_r4(two, [record(user=R4_USER)]))["S1"] is False
    twice = la.llm_view(_h_events([ev(101, "llm_start", incident_id=1)]), 1)
    assert passed(la.rows_r4(twice, [record(user=R4_USER)]))["S1"] is False


def test_records_for_and_prompt_output():
    recs = [record(inc=1, sha="1" * 64), record(inc=2, sha="2" * 64), {"incident": "x"}, {}]
    assert [la.prompt_output(r)["system_sha256"] for r in la.records_for(recs, 2)] == ["2" * 64]
    assert la.records_for(recs, 9) == [] and la.prompt_output({}) == {}


# ---------------------------------------------------------------- 사전 조건(실제 Slack 방지)과 가짜 서버 제어

START = {"event": "start", "notify_mode": "dryrun", "llm_enabled": True}


def _patch_env(monkeypatch, start, fake_up=True, base=()):
    monkeypatch.setattr(la.alerts, "check_preconditions", lambda window_sec: list(base))
    monkeypatch.setattr(la.alerts, "watcher_events", lambda *a: [] if start is None else [start])
    monkeypatch.setattr(la, "fake_running", lambda: fake_up)


def test_preconditions_pass_for_dryrun_llm_and_fake(monkeypatch):
    _patch_env(monkeypatch, START)
    assert la.llm_preconditions("g", 60) == []


@pytest.mark.parametrize("start", [{**START, "notify_mode": "slack"}, {**START, "notify_mode": "discord"},
                                   {"event": "start", "llm_enabled": True}])    # notify_mode 없음도 안전하게 거부
def test_preconditions_refuse_when_not_dryrun(monkeypatch, start):
    _patch_env(monkeypatch, start)
    problems = la.llm_preconditions("f", 60)
    assert len(problems) == 1 and "dryrun" in problems[0] and "Slack" in problems[0]


def test_preconditions_other_problems(monkeypatch):
    _patch_env(monkeypatch, {**START, "llm_enabled": False})
    assert "llm_enabled" in la.llm_preconditions("f", 60)[0]
    _patch_env(monkeypatch, None)
    assert "start 이벤트" in la.llm_preconditions("f", 60)[0]
    _patch_env(monkeypatch, START, fake_up=False)
    assert la.llm_preconditions("f", 60) == []                      # F는 가짜 서버가 없어도 된다
    assert "fake-ollama" in la.llm_preconditions("h", 60)[0]
    _patch_env(monkeypatch, START, base=["traffic 서비스가 실행 중이 아님"])
    assert la.llm_preconditions("r4", 60) == ["traffic 서비스가 실행 중이 아님"]


def test_set_fake_mode_checks_the_answer(monkeypatch):
    def fake_exec(*args):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"mode": args[1], "delay": 90.0}), stderr="")
    monkeypatch.setattr(la, "fake_exec", fake_exec)
    assert la.set_fake_mode("slow", 90) is True
    monkeypatch.setattr(la, "fake_exec", lambda *a: SimpleNamespace(returncode=1, stdout="", stderr="no such service"))
    assert la.set_fake_mode("analysis") is False
    monkeypatch.setattr(la, "fake_exec", lambda *a: SimpleNamespace(returncode=0, stdout="not json", stderr=""))
    assert la.fake_mode() is None and la.set_fake_mode("analysis") is False
