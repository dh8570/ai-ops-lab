import json
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from statistics import median

from config import Config
from logreader import LogReader
from notifier import Notifier
from rules import evaluate
from state import IncidentManager, Store
from stats import summarize


def emit(event: str, **fields) -> None:
    """Watcher 자신의 동작도 JSON 한 줄로 남긴다."""
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main() -> None:
    def _stop(signum, frame):
        emit("stop", signal=signal.Signals(signum).name)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)                            # docker stop / restart 시 즉시 정상 종료

    cfg = Config()
    store = Store(cfg.state_path)
    manager = IncidentManager(store, cfg.service, cfg.resolve_windows)
    notifier = Notifier(cfg.notify_provider, cfg.webhook_url, emit)

    # 재시작 전 상태 복원: 읽던 위치, 기준선 기록, 마지막으로 읽은 시각을 한 묶음으로
    ckpt = store.get_meta("checkpoint", {})
    reader = LogReader(cfg.log_path)
    reader.restore(ckpt.get("reader", {}))
    history: deque[float] = deque(ckpt.get("history", []), maxlen=cfg.r3_baseline_windows)
    last_read_at = ckpt.get("last_read_at")                         # epoch 초, 없으면 None

    open_inc = store.open_incident(cfg.service)
    emit("start",
         window_sec=cfg.window_sec,
         resolve_windows=cfg.resolve_windows,
         restored_offset=reader.offset,
         restored_history=len(history),
         open_incident=open_inc.id if open_inc else None,
         notify_mode=cfg.notify_provider if cfg.webhook_url else "dryrun")

    next_tick = time.monotonic() + cfg.window_sec
    while True:
        time.sleep(max(0, next_tick - time.monotonic()))
        next_tick += cfg.window_sec                                 # 예정 시각 기준, 지연 누적 방지

        # 1. 새 로그 읽기 + 실제 읽은 기간 기준으로 집계
        records, bad = reader.read_new()
        now = time.time()
        elapsed = now - last_read_at if last_read_at else cfg.window_sec
        last_read_at = now
        stats = summarize(records, max(elapsed, 1), cfg.exclude_paths)

        # 2. 규칙 판정 (기준선은 과거 NORMAL 창들의 중앙값, 현재 창은 제외)
        baseline = median(history) if len(history) >= cfg.r3_min_windows else None
        findings = evaluate(stats, baseline, cfg)
        fired = [f for f in findings if f.fired]

        # 3. 장애 상태 전이
        action, inc = manager.step(fired)

        emit("window",
             requests=stats.requests,
             errors_5xx=stats.errors_5xx,
             error_rate=round(stats.error_rate, 3),
             p95_ms=round(stats.p95_ms, 1),
             rpm=round(stats.rpm, 1),
             baseline_rpm=None if baseline is None else round(baseline, 1),
             baseline_n=len(history),
             elapsed_sec=round(elapsed, 1),
             fired=[f.rule_id for f in fired],
             details={f.rule_id: f.detail for f in findings},
             bad_lines=bad,
             action=action,
             incident_id=inc.id if inc else None,
             state=inc.state if inc else "NORMAL")

        # 4. 알림은 새 장애(fire)와 장애 종료(resolve) 두 전이에서만
        if action == "fire":
            notifier.send("FIRING", inc, stats=stats, baseline=baseline)
        elif action == "resolve":
            notifier.send("RESOLVED", inc)

        # 5. 열린 장애가 없는 NORMAL 창이고 표본이 충분할 때만 기준선에 반영
        if action == "none" and stats.requests >= cfg.min_requests:
            history.append(stats.rpm)

        # 6. 상태를 한 번에 저장 (원자적으로 기록되어 반쯤 저장된 상태가 생기지 않음)
        store.set_meta("checkpoint", {
            "reader": reader.snapshot(),
            "history": list(history),
            "last_read_at": last_read_at,
        })


if __name__ == "__main__":
    main()