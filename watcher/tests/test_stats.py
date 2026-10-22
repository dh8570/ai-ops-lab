from config import Config
from stats import percentile, summarize


# 백분위수: 표본·빈 목록 경계를 고정한다.
def test_percentile_nearest_rank():
    assert percentile(list(range(1, 11)), 0.95) == 10
    assert percentile([], 0.95) == 0.0


# 프로브(/healthz) 경로는 창 집계에서 제외한다.
def test_summarize_excludes_healthz():
    cfg = Config()
    records = [
        {"path": "/api", "status": 200, "latency_ms": 10},
        {"path": "/healthz", "status": 500, "latency_ms": 9999},
    ]
    stats = summarize(records, cfg.window_sec, cfg.exclude_paths)
    assert stats.requests == 1
    assert stats.errors_5xx == 0


# 재시작 직후 100.1초 창에 501건이 들어가면 rpm이 300.3으로 부풀지 않게 정규화된다.
def test_rpm_normalized_by_elapsed_not_assuming_60s():
    records = [{"path": "/api", "status": 200, "latency_ms": 1} for _ in range(501)]
    stats = summarize(records, 100.1, ())
    assert round(stats.rpm, 1) == 300.3
