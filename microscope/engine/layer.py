"""층 하나의 계산(아홉 단계). Qwen2DecoderLayer + Qwen2Attention + Qwen2MLP와 같은 순서.
capture(name, layer, tensor)로 중간 값을 내보낸다. 내보내는 텐서는 배치 차원을 뗀 모양이다.
"""
from collections.abc import Callable

import torch
import torch.nn.functional as F

from . import ops
from .config import ModelConfig
from .weights import LayerWeights

Capture = Callable[[str, int | None, torch.Tensor], None]


def decoder_layer(
    x: torch.Tensor,
    w: LayerWeights,
    cfg: ModelConfig,
    cos: torch.Tensor,
    sin: torch.Tensor,
    mask: torch.Tensor,
    layer: int,
    capture: Capture | None = None,
):
    """x: [1,T,H]. 반환: (층 출력 [1,T,H], RoPE 뒤 키 [1,Hk,T,D], 값 [1,Hk,T,D])."""

    def cap(name: str, t: torch.Tensor) -> None:
        if capture is not None:
            capture(name, layer, t[0])

    _, t, _ = x.shape
    d = cfg.head_dim

    # 1 정규화(input_layernorm)
    h = ops.rms_norm(x, w.ln1, cfg.rms_norm_eps)
    cap("norm1", h)

    # 2 Q·K·V 투영(bias 있음) → [1,T,heads,D] → [1,heads,T,D]
    q = F.linear(h, w.q_w, w.q_b).view(1, t, -1, d).transpose(1, 2)
    k = F.linear(h, w.k_w, w.k_b).view(1, t, -1, d).transpose(1, 2)
    v = F.linear(h, w.v_w, w.v_b).view(1, t, -1, d).transpose(1, 2)
    cap("q_pre", q)
    cap("k_pre", k)
    cap("v", v)

    # 3 RoPE(위치에 따라 q·k를 회전)
    q, k = ops.apply_rotary(q, k, cos, sin)
    cap("q", q)
    cap("k", k)

    # 4 K·V 저장: 이번(M2-a)에는 Prefill의 K·V를 반환해 기록만 한다(Decode는 M2-b)

    # 5 어텐션(점수 → 마스크 → 소프트맥스 → V 섞기)
    scores, attn, ctx = ops.causal_softmax_attention(q, k, v, mask, cfg.kv_groups, d**-0.5)
    cap("scores", scores)
    cap("attn", attn)
    cap("ctx", ctx)

    # 6 출력 투영(bias 없음) + 잔차
    o = ctx.transpose(1, 2).contiguous().reshape(1, t, -1).contiguous()
    o = F.linear(o, w.o_w)
    cap("attn_out", o)
    r1 = x + o
    cap("resid1", r1)

    # 7 정규화(post_attention_layernorm)
    h2 = ops.rms_norm(r1, w.ln2, cfg.rms_norm_eps)
    cap("norm2", h2)

    # 8 MLP(gate, up, SiLU, down)
    m, gate, up, act = ops.silu_mlp(h2, w.gate_w, w.up_w, w.down_w)
    cap("mlp.gate", gate)
    cap("mlp.up", up)
    cap("mlp.act", act)
    cap("mlp_out", m)

    # 9 잔차
    r2 = r1 + m
    cap("resid2", r2)
    return r2, k, v
