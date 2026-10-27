
from notifier import build_message, to_slack
from probe import Prober
from state import Incident


class _FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# 연속 타임아웃 2회면 R4 firing, 성공 시 해제·연속 실패 0.
def test_probe_fail_threshold_and_recovery(monkeypatch):
    seq = iter([TimeoutError(), TimeoutError(), _FakeResponse(200)])

    def fake_urlopen(url, timeout=0):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    prober = Prober("http://test/healthz", timeout=1.0, fail_threshold=2)

    prober.probe()
    assert prober.firing is False
    assert prober.consecutive_failures == 1

    prober.probe()
    assert prober.firing is True
    assert prober.consecutive_failures == 2

    prober.probe()
    assert prober.firing is False
    assert prober.consecutive_failures == 0


# 창 동안 R4 가 켜진 적이 있으면 take_window_flag 가 한 번 True 후 False.
def test_take_window_flag_once_per_window(monkeypatch):
    outcomes = [TimeoutError(), TimeoutError(), _FakeResponse(200)] * 3

    def fake_urlopen(url, timeout=0):
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    prober = Prober("http://test/healthz", timeout=1.0, fail_threshold=2)
    for _ in range(3):
        prober.probe()
        prober.probe()
        prober.probe()

    assert prober.take_window_flag() is True
    assert prober.take_window_flag() is False


# UPDATE 메시지는 '새로 추가된 신호'가 첫 필드이고, 시각은 opened_at 과 달라야 한다.
def test_build_message_update_field_and_timestamp():
    inc = Incident(
        id=1,
        service="svc",
        state="FIRING",
        severity="critical",
        rules=["R4", "R3"],
        opened_at="2026-01-01T00:00:00+00:00",
        resolved_at=None,
        fire_windows=2,
        quiet_windows=0,
    )
    msg = build_message("UPDATE", inc, added=["R3"])
    assert msg.fields[0] == ("새로 추가된 신호", "R3")
    assert msg.timestamp != inc.opened_at


# Slack 페이로드: text 는 [FIRING] 으로 시작하고 header 블록은 쓰지 않는다.
def test_to_slack_firing_shape():
    inc = Incident(
        id=7,
        service="visit-counter",
        state="FIRING",
        severity="critical",
        rules=["R1"],
        opened_at="2026-01-01T12:00:00+00:00",
        resolved_at=None,
        fire_windows=1,
        quiet_windows=0,
    )
    msg = build_message("FIRING", inc)
    payload = to_slack(msg)
    assert payload["text"].startswith("[FIRING]")
    block_types = [b["type"] for b in payload["attachments"][0]["blocks"]]
    assert "header" not in block_types


# ---- 알림 실패 기록 (notify_failed) ----
WEBHOOK = "https://hooks.example.test/services/T000/B000/SECRET"


def _send_with(monkeypatch, fake_urlopen):
    import json as _json

    from notifier import Notifier
    records = []
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = Notifier("slack", WEBHOOK, lambda event, **kw: records.append({"event": event, **kw}))
    ok = n.send("FIRING", _incident())
    return ok, records, _json.dumps(records)


def _incident():
    return Incident(id=1, service="app", state="FIRING", severity="critical", rules=["R1"],
                    opened_at="2026-10-26T00:00:00+00:00", resolved_at=None, fire_windows=1, quiet_windows=0)


# DNS 실패 3번 → attempts 3개, 각 항목에 예외 종류와 메시지
def test_notify_failed_records_each_attempt(monkeypatch):
    import socket
    import urllib.error

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))

    ok, records, _ = _send_with(monkeypatch, fake_urlopen)
    assert ok is False
    failed = [r for r in records if r["event"] == "notify_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "URLError"
    assert "total_sec" in failed[0]
    assert len(failed[0]["attempts"]) == 3
    for i, a in enumerate(failed[0]["attempts"], start=1):
        assert a["n"] == i
        assert a["kind"] == "gaierror"
        assert "name resolution" in a["reason"]
        assert isinstance(a["sec"], float)


# 예외 메시지에 웹훅 주소가 들어 있어도 기록 어디에도 남지 않는다
def test_notify_failed_never_contains_webhook(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise OSError(f"cannot connect to {WEBHOOK}")

    ok, records, dumped = _send_with(monkeypatch, fake_urlopen)
    assert ok is False
    assert WEBHOOK not in dumped
    assert "SECRET" not in dumped
    assert "<webhook>" in dumped


# 성공하면 notify_failed 는 emit 되지 않는다
def test_notify_success_emits_no_failure(monkeypatch):
    ok, records, _ = _send_with(monkeypatch, lambda req, timeout=0: _FakeResponse(200))
    assert ok is True
    assert [r["event"] for r in records] == ["notified"]


def _http_error(code, body=b"", headers=None):
    import io
    import urllib.error
    from email.message import Message
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(WEBHOOK, code, "msg", hdrs, io.BytesIO(body))


# 첫 시도 429(Retry-After: 2) → 2초 대기 후 두 번째 시도 성공
def test_notify_429_waits_retry_after_then_succeeds(monkeypatch):
    import json as _json

    from notifier import Notifier
    seq = iter([_http_error(429, b"{}", {"Retry-After": "2"}), _FakeResponse(200)])
    sleeps, records = [], []

    def fake_urlopen(req, timeout=0):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = Notifier("slack", WEBHOOK, lambda event, **kw: records.append({"event": event, **kw}))
    assert n.send("FIRING", _incident()) is True
    assert sleeps == [2.0]
    assert [r["event"] for r in records] == ["notified"]
    assert WEBHOOK not in _json.dumps(records)


# 500 세 번 → notify_failed 한 번, error 는 "HTTP 500", 시도 kind 는 모두 HTTPError
def test_notify_http_500_three_times_records_httperror(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise _http_error(500, b"server exploded")

    ok, records, _ = _send_with(monkeypatch, fake_urlopen)
    assert ok is False
    failed = [r for r in records if r["event"] == "notify_failed"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("HTTP 500")
    assert [a["kind"] for a in failed[0]["attempts"]] == ["HTTPError"] * 3


# ---- send() 는 예외를 밖으로 내보내지 않는다 ----
WEBHOOK_PATH = "/services/T000/B000/SECRET"


def _send_with_sleeps(monkeypatch, fake_urlopen):
    import json as _json

    from notifier import Notifier
    sleeps, records = [], []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = Notifier("slack", WEBHOOK, lambda event, **kw: records.append({"event": event, **kw}))
    ok = n.send("FIRING", _incident())
    return ok, records, sleeps, _json.dumps(records)


# 429 + Retry-After 없음 + 본문이 JSON 아님 → 1초씩 대기하고 세 번 뒤 False
def test_send_429_without_retry_after_and_non_json_body(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise _http_error(429, b"not json")

    ok, records, sleeps, dumped = _send_with_sleeps(monkeypatch, fake_urlopen)
    assert ok is False
    assert [r["event"] for r in records] == ["notify_failed"]
    assert sleeps == [1.0, 1.0, 1.0]
    assert WEBHOOK not in dumped


# Retry-After 가 날짜 형식 → 해석 불가이므로 1초 대기 후 재시도
def test_send_429_with_http_date_retry_after(monkeypatch):
    seq = iter([_http_error(429, b"", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), _FakeResponse(200)])

    def fake_urlopen(req, timeout=0):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    ok, records, sleeps, dumped = _send_with_sleeps(monkeypatch, fake_urlopen)
    assert ok is True
    assert sleeps == [1.0]
    assert WEBHOOK not in dumped


class _BrokenBody:
    def read(self, *args):
        raise TimeoutError("read timed out")

    def close(self):
        pass


def _http_error_broken_body(code):
    import urllib.error
    from email.message import Message
    return urllib.error.HTTPError(WEBHOOK, code, "msg", Message(), _BrokenBody())


# e.read() 가 시간 초과여도 세 번 재시도하고 attempts 가 남는다
def test_send_http_error_body_read_timeout(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise _http_error_broken_body(500)

    ok, records, _, dumped = _send_with_sleeps(monkeypatch, fake_urlopen)
    assert ok is False
    assert len(calls) == 3
    failed = [r for r in records if r["event"] == "notify_failed"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith("HTTP 500")
    assert [a["kind"] for a in failed[0]["attempts"]] == ["HTTPError"] * 3
    assert all(a["reason"].endswith("(본문 읽기 실패: TimeoutError)") for a in failed[0]["attempts"])
    assert "total_sec" in failed[0]
    assert WEBHOOK not in dumped


# 첫 시도는 500 + 본문 읽기 시간 초과, 두 번째 시도는 성공
def test_send_body_read_timeout_then_success(monkeypatch):
    seq = iter([_http_error_broken_body(500), _FakeResponse(200)])

    def fake_urlopen(req, timeout=0):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    ok, records, _, dumped = _send_with_sleeps(monkeypatch, fake_urlopen)
    assert ok is True
    assert [r["event"] for r in records] == ["notified"]
    assert WEBHOOK not in dumped


# 예외 메시지에 웹훅 path 만 들어 있어도 기록 어디에도 남지 않는다
def test_send_scrubs_webhook_path_only(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise OSError(f"bad request target {WEBHOOK_PATH}")

    ok, records, _, dumped = _send_with_sleeps(monkeypatch, fake_urlopen)
    assert ok is False
    assert WEBHOOK_PATH not in dumped
    assert "SECRET" not in dumped
    assert WEBHOOK not in dumped
    assert "<webhook>" in dumped
