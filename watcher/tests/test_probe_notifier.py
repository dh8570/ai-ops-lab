
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
