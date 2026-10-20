import math
from dataclasses import dataclass


@dataclass
class WindowStats:
    requests: int
    errors_5xx: int
    p95_ms: float
    rpm: float

    @property
    def error_rate(self) -> float:
        return self.errors_5xx / self.requests if self.requests else 0.0


def percentile(values: list[float], q: float) -> float:
    """nearest-rank 방식 백분위수."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def summarize(records: list[dict], window_sec: int, exclude_paths: tuple = ()) -> WindowStats:
    rs = [r for r in records if r.get("path") not in exclude_paths]
    latencies = [float(r.get("latency_ms", 0)) for r in rs]
    errors = sum(1 for r in rs if int(r.get("status", 0)) >= 500)
    return WindowStats(
        requests=len(rs),
        errors_5xx=errors,
        p95_ms=percentile(latencies, 0.95),
        rpm=len(rs) * 60 / window_sec,          # 창 길이와 무관하게 분당 건수로 정규화
    )