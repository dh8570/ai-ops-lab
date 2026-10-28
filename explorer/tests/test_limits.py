"""입력 제한: 8001자, num_predict 513, 목록에 없는 model 등은 거부한다."""
import pytest

import limits as L

MODELS = ["qwen2.5:7b"]


def ok(**kw):
    return L.validate_run_request({"prompt": "안녕", "model": "qwen2.5:7b", **kw}, MODELS)


def test_prompt_length_boundary():
    assert L.validate_text("가" * 8000) == "가" * 8000
    with pytest.raises(L.InvalidInput, match="8001"):
        L.validate_text("가" * 8001)


def test_prompt_empty_or_wrong_type_rejected():
    for bad in ("", "   \n", None, 123, ["a"]):
        with pytest.raises(L.InvalidInput):
            L.validate_text(bad)


def test_lone_surrogate_rejected():
    with pytest.raises(L.InvalidInput):
        L.validate_text("abc\ud800")


def test_num_predict_boundaries():
    assert ok(num_predict=1)["options"]["num_predict"] == 1
    assert ok(num_predict=512)["options"]["num_predict"] == 512
    for bad in (513, 0, -1, 1.5, "10", True, None):
        with pytest.raises(L.InvalidInput, match="num_predict"):
            ok(num_predict=bad)


def test_unknown_model_rejected():
    with pytest.raises(L.InvalidInput, match="model"):
        L.validate_run_request({"prompt": "x", "model": "llama3:70b"}, MODELS)
    with pytest.raises(L.InvalidInput, match="model"):
        L.validate_run_request({"prompt": "x"}, MODELS)


def test_num_ctx_choices():
    for good in (2048, 4096, 8192):
        assert ok(num_ctx=good)["options"]["num_ctx"] == good
    for bad in (1024, 16384, "4096", 4096.0):
        with pytest.raises(L.InvalidInput, match="num_ctx"):
            ok(num_ctx=bad)


def test_temperature_range():
    assert ok(temperature=0)["options"]["temperature"] == 0.0
    assert ok(temperature=1.5)["options"]["temperature"] == 1.5
    for bad in (1.6, -0.1, float("nan"), float("inf"), "0.5", True):
        with pytest.raises(L.InvalidInput, match="temperature"):
            ok(temperature=bad)


def test_seed_optional_and_validated():
    assert "seed" not in ok()["options"]               # 비우면 보내지 않는다(Ollama가 임의로 정함)
    assert "seed" not in ok(seed=None)["options"]
    assert ok(seed=42)["options"]["seed"] == 42
    for bad in (-1, 2**31, 1.5, "7", True):
        with pytest.raises(L.InvalidInput, match="seed"):
            ok(seed=bad)


def test_defaults_applied():
    o = ok()["options"]
    assert o == {"num_predict": 128, "num_ctx": 4096, "temperature": 0.7}
