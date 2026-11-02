"""A1-T3b: send 반환값, 종료 때 알림 큐 비우기(close), 로그 집계가 없을 때 창 줄 빼기."""
import ast
import threading
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import analysis
from config import Config
from dispatcher import Dispatcher, LlmRequest
from notifier import Notifier
from state import Incident
from stats import WindowStats

WATCHER_DIR = Path(__file__).resolve().parent.parent
WEBHOOK = "https://hooks.example.invalid/T000/B000/xxxx"
NOW = 1_800_000_000.0


class Events:
    def __init__(self):
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, event, **fields):
        with self._lock:
            self.items.append({"event": event, **fields})

    def of(self, event) -> list[dict]:
        with self._lock:
            return [e for e in self.items if e["event"] == event]


class _FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _inc(id=1) -> Incident:
    return Incident(id, "visit-counter", "FIRING", "critical", ["R1"], "2026-11-02T00:00:00+00:00", None, 1, 0)


# ---------------------------------------------------------------- 1) Notifier.send 반환값

def _send(monkeypatch, url, outcomes):
    """outcomes를 차례로 urlopen 결과로 쓴다(예외면 던진다). (반환값, 이벤트 이름 목록)."""
    events = Events()
    seq = iter(outcomes)

    def fake_urlopen(req, timeout=0):
        o = next(seq)
        if isinstance(o, BaseException):
            raise o
        return o

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok = Notifier("slack", url, events).send("FIRING", _inc())
    return ok, [e["event"] for e in events.items]


def test_send_returns_true_on_success(monkeypatch):
    assert _send(monkeypatch, WEBHOOK, [_FakeResponse(200)]) == (True, ["notified"])


def test_send_returns_true_after_retry(monkeypatch):
    ok, names = _send(monkeypatch, WEBHOOK, [urllib.error.URLError(OSError("down")), _FakeResponse(200)])
    assert (ok, names) == (True, ["notified"])


def test_send_returns_false_when_all_attempts_fail(monkeypatch):
    ok, names = _send(monkeypatch, WEBHOOK, [urllib.error.URLError(OSError("down"))] * 3)
    assert (ok, names) == (False, ["notify_failed"])


def test_send_returns_true_on_dryrun(monkeypatch):
    assert _send(monkeypatch, "", []) == (True, ["notify_dryrun"])


# ---------------------------------------------------------------- 3) close: 종료 때 알림 큐 비우기

class SlowNotifier:
    url = ""

    def __init__(self, delay):
        self.delay, self.calls = delay, []

    def send(self, kind, inc, **ctx):
        time.sleep(self.delay)
        self.calls.append((kind, inc.id))
        return True


class StuckNotifier:
    """gate가 열릴 때까지(최대 60초) 멈춘다."""
    url = ""

    def __init__(self):
        self.gate = threading.Event()

    def send(self, kind, inc, **ctx):
        self.gate.wait(60)
        return True


def _dispatcher(tmp_path, notifier, events, start=True, **kw) -> Dispatcher:
    base = dict(llm_enabled=False, llm_trace_path=str(tmp_path / "llm_runs.jsonl"))
    base.update(kw)
    d = Dispatcher(Config(**base), notifier, events)
    if start:
        d.start(warmup=False)
    return d


def test_close_delivers_queued_notifications(tmp_path):
    events, n = Events(), SlowNotifier(0.2)
    d = _dispatcher(tmp_path, n, events)
    for i in range(3):
        d.submit("UPDATE", _inc(i))
    result = d.close()
    assert result == {"drained": True, "remaining": 0, "llm_pending": 0}
    assert n.calls == [("UPDATE", 0), ("UPDATE", 1), ("UPDATE", 2)]
    assert events.of("shutdown_drain") == [{"event": "shutdown_drain", **result}]


def test_close_gives_up_after_timeout(tmp_path):
    events, n = Events(), StuckNotifier()
    d = _dispatcher(tmp_path, n, events)
    try:
        for i in range(3):
            d.submit("UPDATE", _inc(i))
        t0 = time.monotonic()
        result = d.close(timeout=1.0)
        assert time.monotonic() - t0 < 1.5
        assert result["drained"] is False
        assert result["remaining"] == 3                     # 보내는 중인 1건 + 대기 2건
    finally:
        n.gate.set()


def test_close_reports_pending_llm_requests(tmp_path):
    events = Events()
    d = _dispatcher(tmp_path, SlowNotifier(0), events, start=False, llm_enabled=True)   # LLM 스레드 없이 큐만 채운다
    for i in range(4):
        d.llm_queue.put_nowait(LlmRequest(i, "window", {}))
    assert d.close(timeout=0.2) == {"drained": True, "remaining": 0, "llm_pending": 4}


def test_close_twice_is_safe_and_second_returns_at_once(tmp_path):
    events, n = Events(), StuckNotifier()
    d = _dispatcher(tmp_path, n, events)
    try:
        d.submit("UPDATE", _inc(1))
        first = d.close(timeout=0.3)
        t0 = time.monotonic()
        second = d.close(timeout=5.0)
        assert time.monotonic() - t0 < 0.1
        assert first == second == {"drained": False, "remaining": 1, "llm_pending": 0}
        assert len(events.of("shutdown_drain")) == 1
    finally:
        n.gate.set()


def test_main_stop_handler_closes_dispatcher_before_stop_and_exit():
    """_stop은 main() 안의 지역 함수라 직접 부르기 어렵다(부르면 sys.exit). 소스에서 호출 순서를 확인한다."""
    tree = ast.parse((WATCHER_DIR / "main.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    stop = next(n for n in ast.walk(main_fn) if isinstance(n, ast.FunctionDef) and n.name == "_stop")
    nodes = sorted((n for n in ast.walk(stop) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
    calls = [ast.unparse(n.func) for n in nodes]                # ast.walk는 너비 우선이라 줄 순서로 정렬한다
    calls = [c for c in calls if c in ("dispatcher.close", "emit", "sys.exit")]
    assert calls == ["dispatcher.close", "emit", "sys.exit"]


# ---------------------------------------------------------------- 4) 로그 집계가 없으면 창 줄 빼기

def _evidence(records):
    opened = datetime.fromtimestamp(NOW - 120, UTC).isoformat(timespec="seconds")
    inc = Incident(7, "visit-counter", "FIRING", "critical", ["R4"], opened, None, 1, 0)
    return analysis.build_evidence(inc, WindowStats(0, 0, 0.0, 0.0), records, 300.0,
                                   {"consecutive_failures": 2, "detail": "timeout"}, NOW, current_rules=["R4"])


def test_window_line_is_omitted_without_records():
    system, user = (m["content"] for m in analysis.build_messages(_evidence(None), "런북"))
    assert "[최근" not in user
    assert "[참고] 이 장애는 로그 창이 닫히기 전에 열려" in user
    assert system == analysis.SYSTEM_PROMPT_V1 + "\n\n런북"


def test_window_line_is_kept_with_records():
    ev = _evidence([])
    user = analysis.build_messages(ev, "런북")[1]["content"]
    assert "[최근 60초] 요청 0" in user
    assert "[참고]" not in user
    assert set(ev) == set(_evidence(None))                  # evidence 모양은 같다
