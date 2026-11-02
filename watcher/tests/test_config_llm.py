import importlib
import os

import pytest

import config
from config import Config

LLM_FIELDS = {
    "ollama_url": "http://10.97.0.1:11434", "llm_enabled": False, "llm_model": "qwen2.5:7b",
    "llm_connect_timeout": 5.0, "llm_timeout": 30.0, "llm_hold_sec": 0.0, "llm_keep_alive": "24h",
    "llm_temperature": 0.0, "llm_seed": 42, "llm_top_k": 40, "llm_top_p": 0.9, "llm_repeat_penalty": 1.0,
    "llm_num_predict": 512, "llm_num_ctx": 4096,
}


@pytest.fixture
def load_config(monkeypatch):
    """환경 변수를 정해 config 모듈을 다시 읽는다(기본값은 import 때 환경에서 정해지므로). 끝나면 원래대로 되돌린다."""
    def _load(**env):
        for k in [k for k in os.environ if k.startswith(("LLM_", "OLLAMA_"))]:
            monkeypatch.delenv(k)
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        return importlib.reload(config)
    yield _load
    monkeypatch.undo()
    importlib.reload(config)


def test_defaults(load_config):
    cfg = load_config().Config()
    for name, expected in LLM_FIELDS.items():
        assert getattr(cfg, name) == expected, name
        assert type(getattr(cfg, name)) is type(expected), name


def test_env_overrides(load_config):
    cfg = load_config(
        OLLAMA_URL="http://127.0.0.1:9999", LLM_ENABLED="1", LLM_MODEL="m", LLM_CONNECT_TIMEOUT_SEC="2",
        LLM_TIMEOUT_SEC="10", LLM_HOLD_SEC="1.5", LLM_KEEP_ALIVE="5m", LLM_TEMPERATURE="0.7", LLM_SEED="7",
        LLM_TOP_K="10", LLM_TOP_P="1", LLM_REPEAT_PENALTY="1.1", LLM_NUM_PREDICT="256", LLM_NUM_CTX="2048",
    ).Config()
    assert (cfg.ollama_url, cfg.llm_enabled, cfg.llm_model) == ("http://127.0.0.1:9999", True, "m")
    assert (cfg.llm_connect_timeout, cfg.llm_timeout, cfg.llm_hold_sec, cfg.llm_keep_alive) == (2.0, 10.0, 1.5, "5m")
    assert (cfg.llm_temperature, cfg.llm_seed, cfg.llm_top_k, cfg.llm_top_p) == (0.7, 7, 10, 1.0)
    assert (cfg.llm_repeat_penalty, cfg.llm_num_predict, cfg.llm_num_ctx) == (1.1, 256, 2048)


def test_llm_is_off_by_default_and_existing_fields_are_unchanged():
    cfg = Config()
    assert cfg.llm_enabled is False
    assert (cfg.window_sec, cfg.min_requests, cfg.r1_error_rate) == (60, 20, 0.20)


@pytest.mark.parametrize("raw,expected", [("0", False), ("1", True), ("true", True), ("OFF", False), ("yes", True)])
def test_llm_enabled_parsing(load_config, raw, expected):
    assert load_config(LLM_ENABLED=raw).Config().llm_enabled is expected


def test_llm_enabled_rejects_garbage_naming_the_variable(load_config):
    with pytest.raises(ValueError, match="LLM_ENABLED"):
        load_config(LLM_ENABLED="maybe")


@pytest.mark.parametrize("kw,env", [
    ({"ollama_url": "http://host"}, "OLLAMA_URL"),
    ({"ollama_url": "https://host:1"}, "OLLAMA_URL"),
    ({"ollama_url": "http://host:1/x"}, "OLLAMA_URL"),
    ({"llm_model": "  "}, "LLM_MODEL"),
    ({"llm_connect_timeout": 0}, "LLM_CONNECT_TIMEOUT_SEC"),
    ({"llm_connect_timeout": -1}, "LLM_CONNECT_TIMEOUT_SEC"),
    ({"llm_timeout": 0}, "LLM_TIMEOUT_SEC"),
    ({"llm_timeout": float("inf")}, "LLM_TIMEOUT_SEC"),
    ({"llm_timeout": float("nan")}, "LLM_TIMEOUT_SEC"),
    ({"llm_hold_sec": -1}, "LLM_HOLD_SEC"),
    ({"llm_hold_sec": 3601}, "LLM_HOLD_SEC"),
    ({"llm_keep_alive": ""}, "LLM_KEEP_ALIVE"),
    ({"llm_keep_alive": "forever"}, "LLM_KEEP_ALIVE"),
    ({"llm_temperature": -0.1}, "LLM_TEMPERATURE"),
    ({"llm_temperature": 2.1}, "LLM_TEMPERATURE"),
    ({"llm_temperature": float("nan")}, "LLM_TEMPERATURE"),
    ({"llm_seed": -1}, "LLM_SEED"),
    ({"llm_seed": 2**31}, "LLM_SEED"),
    ({"llm_top_k": 0}, "LLM_TOP_K"),
    ({"llm_top_k": 1001}, "LLM_TOP_K"),
    ({"llm_top_p": 0}, "LLM_TOP_P"),
    ({"llm_top_p": 1.01}, "LLM_TOP_P"),
    ({"llm_repeat_penalty": 0.4}, "LLM_REPEAT_PENALTY"),
    ({"llm_repeat_penalty": 2.1}, "LLM_REPEAT_PENALTY"),
    ({"llm_num_predict": 0}, "LLM_NUM_PREDICT"),
    ({"llm_num_predict": 2049}, "LLM_NUM_PREDICT"),
    ({"llm_num_ctx": 511}, "LLM_NUM_CTX"),
    ({"llm_num_ctx": 8193}, "LLM_NUM_CTX"),
])
def test_out_of_range_stops_startup_naming_the_variable(kw, env):
    with pytest.raises(ValueError, match=env):
        Config(**kw)


@pytest.mark.parametrize("kw", [
    {"llm_temperature": 0}, {"llm_temperature": 2}, {"llm_top_p": 1}, {"llm_top_p": 0.01}, {"llm_seed": 0},
    {"llm_num_predict": 1}, {"llm_num_predict": 2048}, {"llm_num_ctx": 512}, {"llm_num_ctx": 8192},
    {"llm_keep_alive": "0"}, {"llm_keep_alive": "-1"}, {"llm_keep_alive": "1h30m"}, {"llm_hold_sec": 3600},
])
def test_boundary_values_accepted(kw):
    Config(**kw)


def test_bad_env_value_stops_at_load_time(load_config):
    with pytest.raises(ValueError):
        load_config(LLM_TOP_K="abc")
    with pytest.raises(ValueError, match="LLM_TEMPERATURE"):
        load_config(LLM_TEMPERATURE="5").Config()
