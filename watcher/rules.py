from dataclasses import dataclass

from config import Config
from stats import WindowStats


@dataclass
class Finding:
    rule_id: str
    severity: str
    fired: bool
    detail: str


def evaluate(s: WindowStats, baseline_rpm: float | None, cfg: Config) -> list[Finding]:
    enough = s.requests >= cfg.min_requests                    # 표본이 너무 적으면 판단 보류

    r1 = enough and s.error_rate > cfg.r1_error_rate
    r2 = enough and s.p95_ms > cfg.r2_p95_ms

    if baseline_rpm is None:
        r3, r3_detail = False, "baseline 수집 중"
    else:
        r3 = (baseline_rpm >= cfg.r3_min_baseline_rpm
              and s.rpm < cfg.r3_drop_ratio * baseline_rpm)
        r3_detail = f"rpm={s.rpm:.0f} baseline={baseline_rpm:.0f}"

    return [
        Finding("R1", "critical", r1, f"error_rate={s.error_rate:.1%} ({s.errors_5xx}/{s.requests})"),
        Finding("R2", "warning", r2, f"p95={s.p95_ms:.0f}ms"),
        Finding("R3", "warning", r3, r3_detail),
    ]