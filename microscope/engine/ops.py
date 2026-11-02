"""작은 계산들. 설치된 transformers 5.19.0 modeling_qwen2.py의 연산 순서와 자료형 규칙을 그대로 따른다.
모든 함수는 입력 텐서의 자료형(bf16 또는 float32)을 따라 같은 코드로 돈다.
텐서 모양은 HF와 같게 배치 차원 1을 앞에 둔다: hidden [1,T,H], 헤드별 [1,heads,T,D].
"""
import torch
import torch.nn.functional as F


def rms_norm(x: torch.Tensor, weight: torch.Tensor, eps: float) -> torch.Tensor:
    """Qwen2RMSNorm: float32로 올려 계산 → 입력 자료형으로 되돌림 → 그 뒤에 가중치를 곱한다(곱셈은 입력 자료형)."""
    in_dtype = x.dtype
    h = x.to(torch.float32)
    variance = h.pow(2).mean(-1, keepdim=True)
    h = h * torch.rsqrt(variance + eps)
    return weight * h.to(in_dtype)


def rope_inv_freq(head_dim: int, theta: float) -> torch.Tensor:
    """compute_default_rope_parameters와 같은 식. 항상 float32."""
    return 1.0 / (theta ** (torch.arange(0, head_dim, 2, dtype=torch.float) / head_dim))


def rope_cos_sin(positions: torch.Tensor, head_dim: int, theta: float, dtype: torch.dtype):
    """Qwen2RotaryEmbedding.forward: 각도·cos·sin은 float32로 계산하고 마지막에 dtype으로 바꾼다.
    positions: [1,T] 정수. 반환: cos, sin [1,T,D]."""
    inv_freq = rope_inv_freq(head_dim, theta)
    freqs = positions[..., None].float() * inv_freq
    emb = torch.cat((freqs, freqs), dim=-1)
    # HF는 여기서 attention_scaling(기본 RoPE는 1.0)을 곱한다. 1.0 곱셈은 값을 바꾸지 않으므로 생략.
    return emb.cos().to(dtype), emb.sin().to(dtype)


def rotate_half(x: torch.Tensor) -> torch.Tensor:
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary(q: torch.Tensor, k: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor):
    """apply_rotary_pos_emb: q,k [1,heads,T,D], cos,sin [1,T,D]. 계산은 q·k의 자료형으로 한다."""
    cos = cos.unsqueeze(1)
    sin = sin.unsqueeze(1)
    return (q * cos) + (rotate_half(q) * sin), (k * cos) + (rotate_half(k) * sin)


def repeat_kv(x: torch.Tensor, n_rep: int) -> torch.Tensor:
    """[1,Hk,T,D] → [1,Hk*n_rep,T,D]. KV 헤드 j가 질문 헤드 j*n_rep ~ j*n_rep+n_rep-1에 연달아 쓰인다."""
    b, hk, t, d = x.shape
    if n_rep == 1:
        return x
    return x[:, :, None, :, :].expand(b, hk, n_rep, t, d).reshape(b, hk * n_rep, t, d)


def causal_mask(t_q: int, t_k: int, dtype: torch.dtype) -> torch.Tensor:
    """masking_utils.eager_mask와 같은 덧셈 마스크 [1,1,Tq,Tk]: 볼 수 있는 곳 0, 미래는 dtype의 최솟값.
    Prefill에서는 질문 위치 i가 키 위치 0..i만 본다(키 오프셋 t_k - t_q)."""
    qi = torch.arange(t_q)[:, None] + (t_k - t_q)
    kj = torch.arange(t_k)[None, :]
    allowed = kj <= qi
    m = torch.where(allowed, torch.tensor(0.0, dtype=dtype), torch.finfo(dtype).min)
    return m[None, None]


def causal_softmax_attention(q, k, v, mask, n_rep: int, scaling: float):
    """eager_attention_forward와 같은 순서.
    점수 = (q·kᵀ) × scaling + mask (q의 자료형), 소프트맥스는 float32로 계산해 q의 자료형으로 되돌림, 그 뒤 V를 섞는다.
    반환: scores(소프트맥스 전) [1,Hq,T,Tk], attn [1,Hq,T,Tk], ctx [1,Hq,T,D]."""
    k_rep = repeat_kv(k, n_rep)
    v_rep = repeat_kv(v, n_rep)
    scores = torch.matmul(q, k_rep.transpose(2, 3)) * scaling
    if mask is not None:
        scores = scores + mask
    attn = F.softmax(scores, dim=-1, dtype=torch.float32).to(q.dtype)
    ctx = torch.matmul(attn, v_rep)
    return scores, attn, ctx


def silu_mlp(x, gate_w, up_w, down_w):
    """Qwen2MLP: down(silu(gate(x)) * up(x)). 모든 연산이 입력 자료형.
    반환: 출력과 안쪽 값(gate, up, act = silu(gate)*up)."""
    gate = F.linear(x, gate_w)
    up = F.linear(x, up_w)
    act = F.silu(gate) * up
    return F.linear(act, down_w), gate, up, act
