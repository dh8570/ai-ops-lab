import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    log_path: str = os.environ.get("LOG_PATH", "/var/log/app/app.jsonl")
    window_sec: int = _i("WINDOW_SEC", 60)
    min_requests: int = _i("MIN_REQUESTS", 20)
    r1_error_rate: float = _f("R1_ERROR_RATE", 0.20)
    r2_p95_ms: float = _f("R2_P95_MS", 2000)
    r3_drop_ratio: float = _f("R3_DROP_RATIO", 0.7)
    r3_baseline_windows: int = _i("R3_BASELINE_WINDOWS", 10)
    r3_min_windows: int = _i("R3_MIN_WINDOWS", 3)
    r3_min_baseline_rpm: float = _f("R3_MIN_BASELINE_RPM", 60)
    exclude_paths: tuple = field(default_factory=lambda: tuple(
        p for p in os.environ.get("EXCLUDE_PATHS", "/healthz").split(",") if p))