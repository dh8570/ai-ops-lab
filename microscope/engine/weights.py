"""safetensors에서 가중치를 직접 읽는다. 이름과 파일 위치는 model.safetensors.index.json에서 읽는다."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import torch
from safetensors import safe_open

from .config import ModelConfig, model_dir

LAYER_RE = re.compile(r"^model\.layers\.(\d+)\.(.+)$")
# 층 하나에 있어야 하는 가중치(이름 뒷부분). q/k/v에는 bias가 있고 o_proj에는 없다(설치된 modeling_qwen2.py와 같음).
LAYER_KEYS = (
    "input_layernorm.weight",
    "self_attn.q_proj.weight", "self_attn.q_proj.bias",
    "self_attn.k_proj.weight", "self_attn.k_proj.bias",
    "self_attn.v_proj.weight", "self_attn.v_proj.bias",
    "self_attn.o_proj.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight", "mlp.up_proj.weight", "mlp.down_proj.weight",
)
GLOBAL_KEYS = ("model.embed_tokens.weight", "model.norm.weight", "lm_head.weight")


@dataclass
class LayerWeights:
    ln1: torch.Tensor
    q_w: torch.Tensor
    q_b: torch.Tensor
    k_w: torch.Tensor
    k_b: torch.Tensor
    v_w: torch.Tensor
    v_b: torch.Tensor
    o_w: torch.Tensor
    ln2: torch.Tensor
    gate_w: torch.Tensor
    up_w: torch.Tensor
    down_w: torch.Tensor

    def to(self, dtype: torch.dtype) -> "LayerWeights":
        return LayerWeights(**{k: v.to(dtype) for k, v in vars(self).items()})


def read_index(path: Path | None = None) -> dict[str, str]:
    """가중치 이름 → 파일 이름."""
    return json.loads(((path or model_dir()) / "model.safetensors.index.json").read_text())["weight_map"]


def check_index(weight_map: dict[str, str], cfg: ModelConfig) -> None:
    """index의 이름 목록이 config와 기대한 구조에 맞는지 확인한다. 남거나 빠진 이름이 있으면 멈춘다."""
    layers: dict[int, set[str]] = {}
    others = set()
    for name in weight_map:
        m = LAYER_RE.match(name)
        if m:
            layers.setdefault(int(m.group(1)), set()).add(m.group(2))
        else:
            others.add(name)
    if sorted(layers) != list(range(cfg.layers)):
        raise ValueError(f"index의 층 번호가 config의 층 수({cfg.layers})와 다르다: {sorted(layers)[:3]}...")
    for i, keys in layers.items():
        if keys != set(LAYER_KEYS):
            raise ValueError(f"층 {i}의 가중치 이름이 다르다: "
                             f"빠짐 {set(LAYER_KEYS) - keys}, 남음 {keys - set(LAYER_KEYS)}")
    if others != set(GLOBAL_KEYS):
        raise ValueError(f"층 밖 가중치 이름이 다르다: "
                         f"빠짐 {set(GLOBAL_KEYS) - others}, 남음 {others - set(GLOBAL_KEYS)}")


def read_tensors(names: list[str], weight_map: dict[str, str], root: Path | None = None) -> dict[str, torch.Tensor]:
    """이름 목록을 파일별로 묶어 safetensors에서 읽는다. 자료형은 파일 그대로(bf16)."""
    root = root or model_dir()
    by_file: dict[str, list[str]] = {}
    for n in names:
        by_file.setdefault(weight_map[n], []).append(n)
    out = {}
    for fname, ns in by_file.items():
        with safe_open(str(root / fname), framework="pt") as f:
            for n in ns:
                out[n] = f.get_tensor(n)
    return out


def _layer_from(t: dict[str, torch.Tensor], i: int) -> LayerWeights:
    def g(k: str) -> torch.Tensor:
        return t[f"model.layers.{i}.{k}"]

    return LayerWeights(
        ln1=g("input_layernorm.weight"),
        q_w=g("self_attn.q_proj.weight"), q_b=g("self_attn.q_proj.bias"),
        k_w=g("self_attn.k_proj.weight"), k_b=g("self_attn.k_proj.bias"),
        v_w=g("self_attn.v_proj.weight"), v_b=g("self_attn.v_proj.bias"),
        o_w=g("self_attn.o_proj.weight"),
        ln2=g("post_attention_layernorm.weight"),
        gate_w=g("mlp.gate_proj.weight"), up_w=g("mlp.up_proj.weight"), down_w=g("mlp.down_proj.weight"),
    )


def load_layer(i: int, cfg: ModelConfig, root: Path | None = None) -> LayerWeights:
    """층 하나만 읽는다(층 단위 float32 검증용)."""
    wm = read_index(root)
    check_index(wm, cfg)
    return _layer_from(read_tensors([f"model.layers.{i}.{k}" for k in LAYER_KEYS], wm, root), i)


@dataclass
class ModelWeights:
    embed: torch.Tensor
    final_norm: torch.Tensor
    lm_head: torch.Tensor
    layers: list[LayerWeights]


def touch(tensors) -> None:
    """safetensors 0.8의 get_tensor는 파일을 mmap으로 열 뿐 아직 읽지 않는다.
    4KB(bf16 2048개) 간격으로 한 번씩 읽어 실제로 메모리에 올린다(적재 시간이 Prefill에 섞이지 않게)."""
    for t in tensors:
        t.reshape(-1)[::2048].float().sum()          # 합은 쓰지 않는다. 페이지를 읽는 것이 목적


def load_all(cfg: ModelConfig, root: Path | None = None) -> ModelWeights:
    """모든 가중치를 한 번 메모리에 올린다(bf16 그대로, 약 14.2GiB, 읽기 전용 파일 매핑)."""
    wm = read_index(root)
    check_index(wm, cfg)
    t = read_tensors(list(wm), wm, root)
    touch(t.values())
    return ModelWeights(
        embed=t["model.embed_tokens.weight"],
        final_norm=t["model.norm.weight"],
        lm_head=t["lm_head.weight"],
        layers=[_layer_from(t, i) for i in range(cfg.layers)],
    )
