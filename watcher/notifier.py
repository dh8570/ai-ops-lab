import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

COLOR = {"FIRING": "#E5484D", "UPDATE": "#F59E0B", "RESOLVED": "#30A46C"}


@dataclass
class Message:
    """플랫폼과 무관한 알림 내용."""
    kind: str
    title: str
    description: str
    fields: list[tuple[str, str]]
    footer: str
    timestamp: str


def _duration(start: str, end: str) -> str:
    sec = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    return f"{sec // 60}분 {sec % 60}초"


def build_message(kind: str, inc, stats=None, baseline=None, probe=None, added=None) -> Message:
    footer = f"incident #{inc.id:04d} · ai-ops-lab watcher"
    if kind in ("FIRING", "UPDATE"):
        fields = [("규칙", ", ".join(inc.rules))]
        if kind == "UPDATE":
            fields.insert(0, ("새로 추가된 신호", ", ".join(added or []) or "심각도 상승"))
        if stats is not None:
            rpm = f"{stats.rpm:.0f} / {baseline:.0f} rpm" if baseline else f"{stats.rpm:.0f} rpm"
            fields += [("요청 (60초)", str(stats.requests)), ("에러율", f"{stats.error_rate:.1%}"),
                       ("p95", f"{stats.p95_ms:,.0f} ms"), ("처리량 / 기준선", rpm)]
        if probe is not None and not probe["ok"]:
            fields.append(("헬스 체크", f"{probe['consecutive_failures']}회 연속 실패 ({probe['detail']})"))
        desc = ("원인 분석 요약은 3주차(LLM)에서 추가됩니다." if kind == "FIRING"
                else "진행 중인 장애에 새로운 신호가 추가됐습니다. 원인이 바뀌었을 수 있습니다.")
        ts = inc.opened_at if kind == "FIRING" else datetime.now(UTC).isoformat(timespec="seconds")
        return Message(kind, f"[{kind}] {inc.severity.upper()} · {inc.service}", desc, fields, footer, ts)
    return Message(kind, f"[RESOLVED] {inc.service}", "5분 연속 정상으로 장애를 닫습니다.",
                   [("지속 시간", _duration(inc.opened_at, inc.resolved_at)),
                    ("장애 창 수", str(inc.fire_windows)), ("관련 규칙", ", ".join(inc.rules))],
                   footer, inc.resolved_at)


def to_slack(m: Message) -> dict:
    epoch = int(datetime.fromisoformat(m.timestamp).timestamp())
    when = f"<!date^{epoch}^{{date_num}} {{time_secs}}|{m.timestamp}>"     # 보는 사람의 시간대로 표시
    return {
        "text": m.title,                                                  # 푸시 알림·검색용 대체 문장
        "attachments": [{
            "color": COLOR[m.kind],
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": m.description}},
                {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in m.fields]},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{m.footer} · {when}"}]},
            ],
        }],
    }


def to_discord(m: Message) -> dict:
    return {"embeds": [{
        "title": m.title, "description": m.description, "color": int(COLOR[m.kind][1:], 16),
        "fields": [{"name": k, "value": v, "inline": True} for k, v in m.fields],
        "footer": {"text": m.footer}, "timestamp": m.timestamp,
    }]}


FORMATTERS = {"slack": to_slack, "discord": to_discord}


class Notifier:
    def __init__(self, provider: str, webhook_url: str, emit):
        if provider not in FORMATTERS:
            raise ValueError(f"지원하지 않는 알림 채널: {provider}")
        self.provider, self.url, self.emit = provider, webhook_url, emit

    def send(self, kind: str, inc, **ctx) -> bool:
        payload = FORMATTERS[self.provider](build_message(kind, inc, **ctx))
        if not self.url:
            self.emit("notify_dryrun", provider=self.provider, kind=kind, incident_id=inc.id, payload=payload)
            return True

        last_error = None
        attempts = []                                                      # 시도별 실패 기록 (원인 추적용)
        started = time.monotonic()
        for attempt in range(3):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(
                    self.url, data=json.dumps(payload).encode(), method="POST",
                    headers={"Content-Type": "application/json", "User-Agent": "ai-ops-lab-watcher/0.2"})
                t0 = time.monotonic()                                      # urlopen 직전부터 잰다
                with urllib.request.urlopen(req, timeout=5) as res:
                    self.emit("notified", provider=self.provider, kind=kind, incident_id=inc.id, status=res.status)
                    return True
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:200]
                last_error = self._scrub(f"HTTP {e.code} {body}", 200)
                attempts.append(self._attempt(attempt, "HTTPError", f"HTTP {e.code} {body}", t0))
                if e.code == 429:                                          # 속도 제한: 알려준 시간만큼 대기
                    retry = e.headers.get("Retry-After") or json.loads(body or "{}").get("retry_after", 1)
                    time.sleep(min(float(retry), 10))
                    continue
            except urllib.error.URLError as e:                             # 실제 원인은 e.reason
                last_error = type(e).__name__
                attempts.append(self._attempt(attempt, type(e.reason).__name__, str(e.reason), t0))
            except Exception as e:
                last_error = type(e).__name__
                attempts.append(self._attempt(attempt, type(e).__name__, str(e), t0))
            time.sleep(2 ** attempt)
        self.emit("notify_failed", provider=self.provider, kind=kind, incident_id=inc.id, error=last_error,
                  attempts=attempts, total_sec=round(time.monotonic() - started, 1))
        return False

    def _scrub(self, text: str, limit: int) -> str:
        """기록에 웹훅 주소가 남지 않게 치환한 뒤 limit자까지 자른다."""
        return text.replace(self.url, "<webhook>")[:limit]

    def _attempt(self, attempt: int, kind: str, reason: str, t0: float) -> dict:
        return {"n": attempt + 1, "kind": kind, "reason": self._scrub(reason, 150),
                "sec": round(time.monotonic() - t0, 1)}
