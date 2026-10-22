import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service       TEXT NOT NULL,
    state         TEXT NOT NULL,              -- FIRING | RECOVERING | RESOLVED
    severity      TEXT NOT NULL,              -- 장애 동안의 최고 심각도
    rules         TEXT NOT NULL,              -- 장애 동안 켜진 규칙 누적 (JSON 배열)
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    resolved_at   TEXT,
    fire_windows  INTEGER NOT NULL DEFAULT 1, -- 규칙이 켜진 창 수
    quiet_windows INTEGER NOT NULL DEFAULT 0  -- 연속으로 조용했던 창 수
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
SEVERITY = {"warning": 1, "critical": 2}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Incident:
    id: int
    service: str
    state: str
    severity: str
    rules: list
    opened_at: str
    resolved_at: str | None
    fire_windows: int
    quiet_windows: int


def age_sec(inc: Incident) -> float:
    """장애가 열린 뒤 지난 시간(초)."""
    return (datetime.now(UTC) - datetime.fromisoformat(inc.opened_at)).total_seconds()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _row(self, row) -> Incident | None:
        if row is None:
            return None
        d = dict(row)
        return Incident(d["id"], d["service"], d["state"], d["severity"], json.loads(d["rules"]),
                        d["opened_at"], d["resolved_at"], d["fire_windows"], d["quiet_windows"])

    def open_incident(self, service: str) -> Incident | None:
        row = self.db.execute(
            "SELECT * FROM incidents WHERE service=? AND state IN ('FIRING','RECOVERING') "
            "ORDER BY id DESC LIMIT 1", (service,)).fetchone()
        return self._row(row)

    def get(self, incident_id: int) -> Incident:
        return self._row(self.db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def create(self, service: str, severity: str, rules: list) -> Incident:
        ts = now_iso()
        cur = self.db.execute(
            "INSERT INTO incidents (service, state, severity, rules, opened_at, updated_at) "
            "VALUES (?, 'FIRING', ?, ?, ?, ?)", (service, severity, json.dumps(rules), ts, ts))
        self.db.commit()
        return self.get(cur.lastrowid)

    def update(self, incident_id: int, **fields) -> Incident:
        if "rules" in fields:
            fields["rules"] = json.dumps(fields["rules"])
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE incidents SET {cols} WHERE id=?", (*fields.values(), incident_id))
        self.db.commit()
        return self.get(incident_id)

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        self.db.commit()


class IncidentManager:
    """창 하나의 판정 결과로 장애 상태를 전이시키고, 무슨 일이 일어났는지 action으로 돌려준다.

    action: none | fire | suppress | refire | escalate | recovering | resolve
    - fire: 새 장애 → FIRING 알림
    - escalate: 열린 장애에 새 규칙 추가 또는 심각도 상승 → 유예 시간이 지났으면 UPDATE 알림
    - resolve: 장애 종료 → RESOLVED 알림
    나머지는 상태만 갱신하고 알림을 보내지 않는다.
    """

    def __init__(self, store: Store, service: str, resolve_windows: int):
        self.store, self.service, self.resolve_windows = store, service, resolve_windows

    def step(self, fired: list) -> tuple[str, Incident | None, list]:
        inc = self.store.open_incident(self.service)

        if fired:
            rules = sorted({f.rule_id for f in fired})
            severity = max((f.severity for f in fired), key=SEVERITY.get)
            if inc is None:
                return "fire", self.store.create(self.service, severity, rules), rules

            added = sorted(set(rules) - set(inc.rules))                     # 이번에 새로 켜진 규칙
            escalated = SEVERITY[severity] > SEVERITY[inc.severity]          # 심각도 상승
            action = "refire" if inc.state == "RECOVERING" else "suppress"
            inc = self.store.update(
                inc.id, state="FIRING",
                rules=sorted(set(inc.rules) | set(rules)),
                severity=max(inc.severity, severity, key=SEVERITY.get),
                fire_windows=inc.fire_windows + 1, quiet_windows=0)
            return ("escalate" if added or escalated else action), inc, added

        if inc is None:
            return "none", None, []
        quiet = inc.quiet_windows + 1
        if quiet >= self.resolve_windows:
            resolved = self.store.update(
                inc.id, state="RESOLVED", quiet_windows=quiet, resolved_at=now_iso(),
            )
            return "resolve", resolved, []
        return "recovering", self.store.update(inc.id, state="RECOVERING", quiet_windows=quiet), []
