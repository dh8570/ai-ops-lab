"""전체 Prefill: 임베딩 → 층 반복 → 마지막 정규화 → 로짓(bf16 경로와 float32 재계산)."""
from dataclasses import dataclass

import torch
import torch.nn.functional as F

from . import ops
from .config import ModelConfig
from .layer import Capture, decoder_layer
from .weights import ModelWeights

LM_HEAD_CHUNK = 8192          # float32 재계산 때 lm_head를 이 행 수씩 float32로 바꿔 메모리를 아낀다


@dataclass
class PrefillResult:
    final_norm: torch.Tensor          # [T,H] 경로 자료형
    logits_last: torch.Tensor         # [vocab] 경로 자료형(bf16 경로면 bf16)
    logits_last_f32: torch.Tensor     # [vocab] float32 재계산
    keys: list[torch.Tensor]          # 층별 [Hk,T,D] RoPE 뒤
    values: list[torch.Tensor]        # 층별 [Hk,T,D]


def lm_head_f32(h_last: torch.Tensor, lm_head: torch.Tensor, chunk: int = LM_HEAD_CHUNK) -> torch.Tensor:
    """마지막 위치의 정규화 출력 [H]를 float32로 올려, lm_head를 chunk행씩 float32로 바꿔 곱한다."""
    x = h_last.to(torch.float32)
    out = torch.empty(lm_head.shape[0], dtype=torch.float32)
    for s in range(0, lm_head.shape[0], chunk):
        out[s : s + chunk] = F.linear(x, lm_head[s : s + chunk].to(torch.float32))
    return out


@torch.no_grad()
def prefill(ids: list[int], w: ModelWeights, cfg: ModelConfig, dtype: torch.dtype = torch.bfloat16,
            capture: Capture | None = None) -> PrefillResult:
    """토큰 번호 목록을 한 번에 읽는다. 가중치가 dtype과 다르면 층마다 바꿔 쓴다(float32 모드)."""
    def cap(name: str, t: torch.Tensor) -> None:
        if capture is not None:
            capture(name, None, t)

    input_ids = torch.tensor([ids], dtype=torch.long)
    t = input_ids.shape[1]
    x = F.embedding(input_ids, w.embed).to(dtype)          # [1,T,H]
    cap("embed", x[0])

    positions = torch.arange(t).unsqueeze(0)               # HF와 같은 position_ids [1,T]
    # HF는 cos·sin을 임베딩(hidden_states)의 자료형으로, 마스크도 같은 자료형으로 만든다
    cos, sin = ops.rope_cos_sin(positions, cfg.head_dim, cfg.rope_theta, x.dtype)
    mask = ops.causal_mask(t, t, x.dtype)

    keys, values = [], []
    for i, lw in enumerate(w.layers):
        if lw.q_w.dtype != dtype:
            lw = lw.to(dtype)
        x, k, v = decoder_layer(x, lw, cfg, cos, sin, mask, i, capture)
        keys.append(k[0])
        values.append(v[0])

    h = ops.rms_norm(x, w.final_norm.to(dtype), cfg.rms_norm_eps)
    cap("final_norm", h[0])

    # HF와 같이 모든 위치의 로짓을 계산한 뒤 마지막 위치를 꺼낸다(행 하나만 계산하면 행렬곱 경로가 달라질 수 있다)
    logits = F.linear(h, w.lm_head.to(dtype))
    logits_last = logits[0, -1].clone()
    del logits
    if dtype == torch.bfloat16:
        cap("logits_last_bf16", logits_last)

    logits_f32 = lm_head_f32(h[0, -1], w.lm_head)
    cap("logits_last_f32", logits_f32)
    return PrefillResult(final_norm=h[0], logits_last=logits_last, logits_last_f32=logits_f32, keys=keys, values=values)


def topk(logits: torch.Tensor, k: int, decode) -> list[dict]:
    """상위 k개: id, 글자, logit, 확률, 로그확률(float32로 계산)."""
    lf = logits.to(torch.float32)
    lp = torch.log_softmax(lf, dim=-1)
    v, idx = lf.topk(k)
    return [{"id": int(i), "text": decode(int(i)), "logit": float(x), "prob": float(lp[i].exp()),
             "logprob": float(lp[i])} for x, i in zip(v, idx, strict=True)]
