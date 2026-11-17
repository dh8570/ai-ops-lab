"""T7b: 비밀·운영 채널 보호.

1) webhook_url repr 숨김 2) 분석을 운영 채널로 보내려면 LLM_ANALYSIS_ON_MAIN=1 명시 3) 합격 시험 가드.
진짜 주소는 쓰지 않는다(가짜 값 SECRET-MARKER만).
"""
import pytest
import test_acceptance_llm as ta  # 합격 시험 모듈을 불러오는 방식은 그 파일을 따른다(임시 폴더에 복사해 import)

from config import Config

SECRET = "https://SECRET-MARKER.example/x"
la = ta.la


# ---------------------------------------------------------------- 1) repr

def test_webhook_url_hidden_from_repr():
    cfg = Config(webhook_url=SECRET)
    assert "SECRET-MARKER" not in repr(cfg) and "SECRET-MARKER" not in str(cfg)
    assert "webhook_url" not in repr(cfg)
    assert cfg.webhook_url == SECRET                      # 읽는 쪽은 그대로


# ---------------------------------------------------------------- 2) 시작 거부 규칙

def test_refuses_llm_on_with_main_webhook_and_no_analysis_webhook():
    with pytest.raises(ValueError) as e:
        Config(llm_enabled=True, webhook_url=SECRET)
    msg = str(e.value)
    assert "LLM_ENABLED" in msg and "NOTIFY_WEBHOOK_URL" in msg and "ANALYSIS_WEBHOOK_URL" in msg
    assert "LLM_ANALYSIS_ON_MAIN=1" in msg
    assert "SECRET-MARKER" not in msg and "SECRET-MARKER" not in repr(e.value)


@pytest.mark.parametrize("kw", [
    {"llm_enabled": True, "webhook_url": SECRET, "llm_analysis_on_main": True},                   # 명시 동의
    {"llm_enabled": True, "webhook_url": SECRET, "analysis_webhook_url": "https://a.example/y"},  # 별도 채널
    {"llm_enabled": True, "webhook_url": ""},                                                     # dryrun
    {"llm_enabled": False, "webhook_url": SECRET},                                                # LLM 꺼짐
    {},                                                                                           # 기본값
])
def test_allowed_combinations(kw):
    Config(**kw)


def test_default_on_main_is_false():
    assert Config().llm_analysis_on_main is False


def test_env_parsing(monkeypatch):
    import importlib

    import config
    for k, v in (("LLM_ENABLED", "1"), ("NOTIFY_WEBHOOK_URL", SECRET)):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_ANALYSIS_ON_MAIN", raising=False)
    try:
        with pytest.raises(ValueError, match="LLM_ANALYSIS_ON_MAIN"):
            importlib.reload(config).Config()
        monkeypatch.setenv("LLM_ANALYSIS_ON_MAIN", "1")
        assert importlib.reload(config).Config().llm_analysis_on_main is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ---------------------------------------------------------------- 3) 합격 시험 가드

@pytest.mark.parametrize("channel,expected_problems", [("separate", 1), ("same", 0), (None, 0)])
def test_acceptance_guard_on_analysis_channel(monkeypatch, channel, expected_problems):
    start = dict(ta.START)
    if channel is not None:                                # None이면 필드 없음(이전 빌드)
        start["analysis_channel"] = channel
    ta._patch_env(monkeypatch, start)
    problems = la.llm_preconditions("g", 60)
    assert len(problems) == expected_problems
    if expected_problems:
        assert "ANALYSIS_WEBHOOK_URL=" in problems[0] and "별도 웹훅" in problems[0]
