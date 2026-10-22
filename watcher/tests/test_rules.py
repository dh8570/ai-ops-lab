from config import Config
from rules import evaluate
from stats import WindowStats


def _fired_rules(stats: WindowStats, baseline: float | None = 300.0) -> list[str]:
    cfg = Config()
    return [f.rule_id for f in evaluate(stats, baseline, cfg) if f.fired]


# 2주차: 5xx 급증 창에서 R1만 켜져야 한다 (지연·처리량은 정상).
def test_r1_only_on_high_error_rate():
    stats = WindowStats(requests=300, errors_5xx=113, p95_ms=100.0, rpm=300.0)
    assert _fired_rules(stats) == ["R1"]


# 2주차: 표본 부족 창에서 오탐하지 않도록 최소 요청 수 미만이면 R1·R2는 꺼진다.
def test_r1_r2_need_minimum_requests():
    stats = WindowStats(requests=10, errors_5xx=10, p95_ms=5000.0, rpm=10.0)
    # 기준선 300에서는 rpm 10이라 R3가 켜지지만, R3는 최소 요청 조건이 없으므로 이 테스트의 관심사가 아니다.
    fired = _fired_rules(stats, baseline=300.0)
    assert "R1" not in fired
    assert "R2" not in fired


# 2-C 앱 완전 정지 실험에서 요청 0건 창의 R3 탐지를 고정
def test_r3_fires_on_near_zero_traffic():
    stats = WindowStats(requests=0, errors_5xx=0, p95_ms=0.0, rpm=0.0)
    assert "R3" in _fired_rules(stats, baseline=300.0)


# 2주차: p95 지연 급증 시 R2만(또는 R2 포함) 켜진다.
def test_r2_on_high_p95():
    stats = WindowStats(requests=300, errors_5xx=0, p95_ms=3000.7, rpm=300.0)
    assert "R2" in _fired_rules(stats)
    assert _fired_rules(stats) == ["R2"]


# 2주차: 기준선 미수집 시 R3는 켜지지 않고 'baseline 수집 중'으로 고정한다.
def test_r3_off_while_baseline_collecting():
    stats = WindowStats(requests=300, errors_5xx=0, p95_ms=100.0, rpm=50.0)
    cfg = Config()
    findings = evaluate(stats, None, cfg)
    r3 = next(f for f in findings if f.rule_id == "R3")
    assert not r3.fired
    assert r3.detail == "baseline 수집 중"


# 2주차: 포화 실측(58%)에서 R3 처리량 급감이 켜진다.
def test_r3_fires_below_drop_ratio():
    stats = WindowStats(requests=300, errors_5xx=0, p95_ms=100.0, rpm=175.0)
    assert "R3" in _fired_rules(stats, baseline=300.0)


# 2주차: 경계 창(90%)에서는 R3가 켜지지 않는다.
def test_r3_silent_at_boundary_ninety_percent():
    stats = WindowStats(requests=300, errors_5xx=0, p95_ms=100.0, rpm=270.0)
    assert _fired_rules(stats, baseline=300.0) == []


# 2주차: 기준선 rpm이 최소값(60) 미만이면 R3를 쓰지 않는다.
def test_r3_off_when_baseline_too_low():
    stats = WindowStats(requests=300, errors_5xx=0, p95_ms=100.0, rpm=30.0)
    assert _fired_rules(stats, baseline=50.0) == []
