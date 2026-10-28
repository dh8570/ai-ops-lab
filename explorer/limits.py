"""입력 제한. 요청을 Ollama에 보내기 전에 여기서 걸러 낸다."""
import math

PROMPT_MAX = 8000                      # 프롬프트 글자 수(코드포인트) 상한
NUM_PREDICT_MIN, NUM_PREDICT_MAX = 1, 512
NUM_CTX_CHOICES = (2048, 4096, 8192)
TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 1.5
SEED_MIN, SEED_MAX = 0, 2**31 - 1

DEFAULTS = {"temperature": 0.7, "num_predict": 128, "num_ctx": 4096}


class InvalidInput(ValueError):
    """사용자에게 그대로 보여 줘도 되는 거절 사유."""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)    # True/False는 int의 하위 타입이라 따로 막는다


def validate_text(text) -> str:
    if not isinstance(text, str):
        raise InvalidInput("text(프롬프트)는 문자열이어야 합니다.")
    if not text.strip():
        raise InvalidInput("프롬프트가 비어 있습니다.")
    if len(text) > PROMPT_MAX:
        raise InvalidInput(f"프롬프트가 너무 깁니다: {len(text)}자 (최대 {PROMPT_MAX}자).")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidInput("UTF-8로 바꿀 수 없는 글자(짝 없는 서로게이트)가 들어 있습니다.") from None
    return text


def validate_run_request(payload: dict, allowed_models) -> dict:
    """/api/run 본문을 검사해 {prompt, model, options}로 돌려준다. 어긋나면 InvalidInput."""
    prompt = validate_text(payload.get("prompt"))

    model = payload.get("model")
    if not isinstance(model, str) or model not in allowed_models:
        raise InvalidInput(f"허용되지 않은 model입니다. 사용 가능: {sorted(allowed_models)}")

    num_predict = payload.get("num_predict", DEFAULTS["num_predict"])
    if not _is_int(num_predict) or not NUM_PREDICT_MIN <= num_predict <= NUM_PREDICT_MAX:
        raise InvalidInput(f"num_predict는 {NUM_PREDICT_MIN}~{NUM_PREDICT_MAX} 정수여야 합니다: {num_predict!r}")

    num_ctx = payload.get("num_ctx", DEFAULTS["num_ctx"])
    if not _is_int(num_ctx) or num_ctx not in NUM_CTX_CHOICES:
        raise InvalidInput(f"num_ctx는 {list(NUM_CTX_CHOICES)} 중 하나여야 합니다: {num_ctx!r}")

    temperature = payload.get("temperature", DEFAULTS["temperature"])
    if (isinstance(temperature, bool) or not isinstance(temperature, int | float)
            or not math.isfinite(temperature) or not TEMPERATURE_MIN <= temperature <= TEMPERATURE_MAX):
        raise InvalidInput(f"temperature는 {TEMPERATURE_MIN}~{TEMPERATURE_MAX} 숫자여야 합니다: {temperature!r}")

    options = {"num_predict": num_predict, "num_ctx": num_ctx, "temperature": float(temperature)}
    seed = payload.get("seed")                              # 비우면(None) 보내지 않는다 -> Ollama가 임의로 정함
    if seed is not None:
        if not _is_int(seed) or not SEED_MIN <= seed <= SEED_MAX:
            raise InvalidInput(f"seed는 {SEED_MIN}~{SEED_MAX} 정수이거나 비워 두세요: {seed!r}")
        options["seed"] = seed
    return {"prompt": prompt, "model": model, "options": options}
