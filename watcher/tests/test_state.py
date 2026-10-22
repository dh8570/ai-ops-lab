from datetime import UTC, datetime, timedelta

from policy import should_notify_update, should_open_from_probe, should_update_baseline
from rules import Finding
from state import IncidentManager, Store


def _mgr(tmp_path):
    return IncidentManager(Store(str(tmp_path / "w.db")), "svc", resolve_windows=5)


def _f(rule_id: str, severity: str = "critical") -> Finding:
    return Finding(rule_id, severity, True, "test")


# R1 두 창 후 조용한 창 6번이면 fire→suppress→recovering×4→resolve→none 순서로 간다.
def test_r1_then_quiet_windows_transitions(tmp_path):
    mgr = _mgr(tmp_path)
    seq = []
    action, inc, _ = mgr.step([_f("R1")])
    seq.append(action)
    action, inc, _ = mgr.step([_f("R1")])
    seq.append(action)
    for _ in range(6):
        action, inc, _ = mgr.step([])
        seq.append(action)
    assert seq == ["fire", "suppress", "recovering", "recovering", "recovering", "recovering", "resolve", "none"]


# R2·R3 동시 warning 이면 fire 1건, rules 와 severity 가 warning 으로 고정된다.
def test_r2_r3_simultaneous_warning_fire(tmp_path):
    mgr = _mgr(tmp_path)
    action, inc, added = mgr.step([_f("R2", "warning"), _f("R3", "warning")])
    assert action == "fire"
    assert inc is not None
    assert inc.rules == ["R2", "R3"]
    assert inc.severity == "warning"
    assert added == ["R2", "R3"]


# recovering 중 같은 규칙 재발이면 refire, id 유지, added 는 비어 있다.
def test_refire_same_rules_after_recovering(tmp_path):
    mgr = _mgr(tmp_path)
    _, inc, _ = mgr.step([_f("R1")])
    first_id = inc.id
    mgr.step([])
    action, inc, added = mgr.step([_f("R1")])
    assert action == "refire"
    assert inc.id == first_id
    assert added == []


# R4 로 연 장애에 R2 가 붙으면 escalate, added ["R2"], rules ["R2","R4"].
def test_escalate_r2_onto_r4_incident(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.step([_f("R4")])
    action, inc, added = mgr.step([_f("R2", "warning")])
    assert action == "escalate"
    assert added == ["R2"]
    assert inc.rules == ["R2", "R4"]


# 같은 DB 경로로 Store 를 새로 만들어도 열린 장애가 유지되어 다음은 suppress.
def test_persisted_open_incident_survives_store_reopen(tmp_path):
    db = tmp_path / "w.db"
    mgr = IncidentManager(Store(str(db)), "svc", resolve_windows=5)
    mgr.step([_f("R1")])
    mgr2 = IncidentManager(Store(str(db)), "svc", resolve_windows=5)
    action, _, _ = mgr2.step([_f("R1")])
    assert action == "suppress"


def _set_opened_at(store: Store, inc_id: int, seconds_ago: float):
    ts = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
    store.db.execute("UPDATE incidents SET opened_at=? WHERE id=?", (ts, inc_id))
    store.db.commit()


# 프로브 장애 75초 뒤 붙은 규칙은 UPDATE 유예(180초) 안이라 알리면 안 된다.
def test_should_notify_update_false_inside_grace(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    mgr = IncidentManager(store, "svc", resolve_windows=5)
    _, inc, _ = mgr.step([_f("R4")])
    _set_opened_at(store, inc.id, 75)
    inc = store.get(inc.id)
    assert should_notify_update("escalate", inc, 180) is False


# 유예 시간을 넘긴 escalate 는 UPDATE 알림 대상이다.
def test_should_notify_update_true_after_grace(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    mgr = IncidentManager(store, "svc", resolve_windows=5)
    _, inc, _ = mgr.step([_f("R4")])
    _set_opened_at(store, inc.id, 1140)
    inc = store.get(inc.id)
    assert should_notify_update("escalate", inc, 180) is True


# 창 사이 R4 즉시 개시: 막 켜졌고 열린 장애 없을 때만 True.
def test_should_open_from_probe():
    store = Store(":memory:")
    mgr = IncidentManager(store, "svc", resolve_windows=5)
    _, open_inc, _ = mgr.step([_f("R4")])
    assert should_open_from_probe(False, True, None) is True
    assert should_open_from_probe(True, True, None) is False
    assert should_open_from_probe(False, True, open_inc) is False


# NORMAL 창이고 표본 충분할 때만 기준선(history)에 rpm 을 넣는다.
def test_should_update_baseline():
    assert should_update_baseline("none", 300, 20) is True
    assert should_update_baseline("none", 0, 20) is False
    assert should_update_baseline("recovering", 2103, 20) is False
