import math
import os
import re
from dataclasses import dataclass, field

from llm_client import validate_base_url


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={raw!r}: 0 또는 1이어야 한다")


_KEEP_ALIVE_RE = re.compile(r"-1|\d+|(\d+(\.\d+)?(ms|s|m|h))+")   # Ollama keep_alive: 24h, 5m, 1h30m, 0, -1


def _need(env: str, value, ok: bool, rule: str) -> None:
    """LLM 설정 하나의 범위 검사. 어기면 어느 환경 변수가 왜 틀렸는지 적어 시작할 때 멈춘다."""
    if not ok:
        raise ValueError(f"{env}={value!r}: {rule}")


def _in(value, lo, hi) -> bool:
    return math.isfinite(value) and lo <= value <= hi


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
    service: str = os.environ.get("SERVICE_NAME", "visit-counter")
    state_path: str = os.environ.get("STATE_PATH", "/data/watcher.db")
    resolve_windows: int = _i("RESOLVE_WINDOWS", 5)          # 연속 조용한 창 수 → RESOLVED (60초 × 5 = 5분)
    notify_provider: str = os.environ.get("NOTIFY_PROVIDER", "slack")
    webhook_url: str = os.environ.get("NOTIFY_WEBHOOK_URL", "")
    probe_url: str = os.environ.get("PROBE_URL", "http://app:8000/healthz")
    probe_interval: int = _i("PROBE_INTERVAL_SEC", 15)
    probe_timeout: float = _f("PROBE_TIMEOUT_SEC", 2.0)
    probe_fail_threshold: int = _i("PROBE_FAIL_THRESHOLD", 2)
    update_grace_sec: int = _i("UPDATE_GRACE_SEC", 180)          # 장애가 열린 뒤 이 시간 안에 추가된 신호는 조용히 합침
    exclude_paths: tuple = field(default_factory=lambda: tuple(
        p for p in os.environ.get("EXCLUDE_PATHS", "/healthz").split(",") if p))

    # --- LLM 분석(A1). llm_enabled가 0이면 아무 동작도 켜지지 않는다 ---
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://10.97.0.1:11434")   # 서버 설정에서만. 사용자 입력 아님
    llm_enabled: bool = _b("LLM_ENABLED", False)
    llm_model: str = os.environ.get("LLM_MODEL", "qwen2.5:7b")
    llm_connect_timeout: float = _f("LLM_CONNECT_TIMEOUT_SEC", 5)   # 연결 제한(초)
    llm_timeout: float = _f("LLM_TIMEOUT_SEC", 30)                  # 연결부터 응답 끝까지 총 상한(초)
    llm_hold_sec: float = _f("LLM_HOLD_SEC", 0)                     # 알림을 붙잡는 시간(초). 0이면 안 붙잡음
    llm_keep_alive: str = os.environ.get("LLM_KEEP_ALIVE", "24h")   # 모델을 메모리에 유지하는 시간
    # 샘플링 옵션 7개: 요청과 기록에 항상 모두 명시한다
    llm_temperature: float = _f("LLM_TEMPERATURE", 0)
    llm_seed: int = _i("LLM_SEED", 42)
    llm_top_k: int = _i("LLM_TOP_K", 40)
    llm_top_p: float = _f("LLM_TOP_P", 0.9)
    llm_repeat_penalty: float = _f("LLM_REPEAT_PENALTY", 1.0)
    llm_num_predict: int = _i("LLM_NUM_PREDICT", 512)
    llm_num_ctx: int = _i("LLM_NUM_CTX", 4096)

    def __post_init__(self):
        try:
            validate_base_url(self.ollama_url)
        except ValueError as e:
            raise ValueError(f"OLLAMA_URL={self.ollama_url!r}: {e}") from e
        _need("LLM_MODEL", self.llm_model, bool(self.llm_model.strip()), "비어 있으면 안 된다")
        _need("LLM_CONNECT_TIMEOUT_SEC", self.llm_connect_timeout,
              math.isfinite(self.llm_connect_timeout) and self.llm_connect_timeout > 0, "0보다 큰 수여야 한다")
        _need("LLM_TIMEOUT_SEC", self.llm_timeout,
              math.isfinite(self.llm_timeout) and self.llm_timeout > 0, "0보다 큰 수여야 한다")
        _need("LLM_HOLD_SEC", self.llm_hold_sec, _in(self.llm_hold_sec, 0, 3600), "0~3600 범위여야 한다")
        _need("LLM_KEEP_ALIVE", self.llm_keep_alive, bool(_KEEP_ALIVE_RE.fullmatch(self.llm_keep_alive)),
              "24h, 5m, 0, -1 같은 형식이어야 한다")
        _need("LLM_TEMPERATURE", self.llm_temperature, _in(self.llm_temperature, 0, 2), "0~2 범위여야 한다")
        _need("LLM_SEED", self.llm_seed, 0 <= self.llm_seed <= 2**31 - 1, "0~2147483647 범위여야 한다")
        _need("LLM_TOP_K", self.llm_top_k, 1 <= self.llm_top_k <= 1000, "1~1000 범위여야 한다")
        _need("LLM_TOP_P", self.llm_top_p, math.isfinite(self.llm_top_p) and 0 < self.llm_top_p <= 1,
              "0 초과 1 이하여야 한다")
        _need("LLM_REPEAT_PENALTY", self.llm_repeat_penalty, _in(self.llm_repeat_penalty, 0.5, 2.0),
              "0.5~2.0 범위여야 한다")
        _need("LLM_NUM_PREDICT", self.llm_num_predict, 1 <= self.llm_num_predict <= 2048, "1~2048 범위여야 한다")
        _need("LLM_NUM_CTX", self.llm_num_ctx, 512 <= self.llm_num_ctx <= 8192, "512~8192 범위여야 한다")
