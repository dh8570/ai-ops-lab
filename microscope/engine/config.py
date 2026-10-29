"""config.json을 읽어 모델 상수를 돌려준다. 상수를 코드에 적지 않는다."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL_DIR = Path.home() / "microscope-spike" / "model"
DEFAULT_REPO = "Qwen/Qwen2.5-7B-Instruct"          # 메타데이터(manifest.model.repo)용. 계산에는 쓰지 않는다


def model_dir() -> Path:
    return Path(os.environ.get("MICROSCOPE_MODEL_DIR", DEFAULT_MODEL_DIR))


def threads() -> int:
    return int(os.environ.get("MICROSCOPE_THREADS", "6"))


@dataclass(frozen=True)
class ModelConfig:
    layers: int
    hidden_size: int
    heads: int
    kv_heads: int
    head_dim: int
    intermediate_size: int
    vocab_size: int
    rms_norm_eps: float
    rope_theta: float
    hidden_act: str

    @property
    def kv_groups(self) -> int:
        """GQA에서 KV 헤드 하나를 질문 헤드 몇 개가 함께 쓰는가(= repeat_kv 반복 수)."""
        return self.heads // self.kv_heads


def load_config(path: Path | None = None) -> ModelConfig:
    d = json.loads(((path or model_dir()) / "config.json").read_text())
    if d.get("model_type") != "qwen2":
        raise ValueError(f"qwen2가 아닌 모델: {d.get('model_type')}")
    if d.get("use_sliding_window"):
        raise ValueError("sliding window 어텐션은 구현하지 않았다")
    if d.get("rope_scaling"):
        raise ValueError("rope_scaling은 구현하지 않았다(기본 RoPE만)")
    if d.get("hidden_act") != "silu":
        raise ValueError(f"silu가 아닌 활성 함수: {d.get('hidden_act')}")
    heads = d["num_attention_heads"]
    return ModelConfig(
        layers=d["num_hidden_layers"],
        hidden_size=d["hidden_size"],
        heads=heads,
        kv_heads=d.get("num_key_value_heads") or heads,
        # HF와 같은 규칙: config에 head_dim이 없으면 hidden_size // heads
        head_dim=d.get("head_dim") or d["hidden_size"] // heads,
        intermediate_size=d["intermediate_size"],
        vocab_size=d["vocab_size"],
        rms_norm_eps=d["rms_norm_eps"],
        rope_theta=d["rope_theta"],
        hidden_act=d["hidden_act"],
    )


def revision(path: Path | None = None) -> str:
    f = (path or model_dir()) / "REVISION.txt"
    return f.read_text().strip() if f.exists() else "unknown"
