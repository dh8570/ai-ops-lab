import json
import time
from collections import deque
from datetime import datetime, timezone

from config import Config
from logreader import LogReader
from statistics import median
from rules import evaluate
from stats import summarize


def emit(event: str, **fields) -> None:
    """Watcher 자신의 동작도 JSON 한 줄로 남긴다."""
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main() -> None:
    cfg = Config()
    reader = LogReader(cfg.log_path)
    history: deque[float] = deque(maxlen=cfg.r3_baseline_windows)   # 정상 창의 rpm만 보관
    emit("start", window_sec=cfg.window_sec, log_path=cfg.log_path, exclude_paths=list(cfg.exclude_paths))

    next_tick = time.monotonic() + cfg.window_sec
    while True:
        time.sleep(max(0, next_tick - time.monotonic()))
        next_tick += cfg.window_sec                                 # 예정 시각 기준, 지연 누적 방지

        records, bad = reader.read_new()
        stats = summarize(records, cfg.window_sec, cfg.exclude_paths)
        baseline = median(history) if len(history) >= cfg.r3_min_windows else None
        findings = evaluate(stats, baseline, cfg)
        fired = [f.rule_id for f in findings if f.fired]

        emit("window",
             requests=stats.requests, errors_5xx=stats.errors_5xx,
             error_rate=round(stats.error_rate, 3), p95_ms=round(stats.p95_ms, 1),
             rpm=round(stats.rpm, 1), baseline_rpm=None if baseline is None else round(baseline, 1),
             baseline_n=len(history),                                  # 판정 시점의 기준선 기록 개수
             fired=fired, details={f.rule_id: f.detail for f in findings}, bad_lines=bad)

        if not fired and stats.requests >= cfg.min_requests:
            history.append(stats.rpm)                # 정상이고 표본이 충분한 창만 기준선에 반영


if __name__ == "__main__":
    main()