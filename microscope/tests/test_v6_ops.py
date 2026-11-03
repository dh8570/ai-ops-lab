"""V6: 작은 연산 단위 시험(모델 없이). 작은 무작위 텐서로 우리 함수와 HF의 같은 함수를 비교한다.
같은 연산 순서를 따랐으므로 float32와 bf16 모두 비트 단위로 같아야 한다(torch.equal)."""
from types import SimpleNamespace

import pytest
import torch
from transformers import Qwen2Config
from transformers.masking_utils import eager_mask
from transformers.models.qwen2 import modeling_qwen2 as hf

from microscope.engine import ops
from microscope.engine.config import ModelConfig
from microscope.engine.layer import decoder_layer
from microscope.engine.weights import LayerWeights

DTYPES = [torch.float32, torch.bfloat16]
T, H, HQ, HK, D, INTER = 7, 64, 4, 2, 16, 96
THETA = 1_000_000.0


def small_config() -> Qwen2Config:
    c = Qwen2Config(hidden_size=H, intermediate_size=INTER, num_attention_heads=HQ, num_key_value_heads=HK,
                    num_hidden_layers=1, vocab_size=32, rms_norm_eps=1e-6,
                    rope_parameters={"rope_type": "default", "rope_theta": THETA})
    c._attn_implementation = "eager"
    return c


def ours_config() -> ModelConfig:
    return ModelConfig(layers=1, hidden_size=H, heads=HQ, kv_heads=HK, head_dim=D, intermediate_size=INTER,
                       vocab_size=32, rms_norm_eps=1e-6, rope_theta=THETA, hidden_act="silu")


@pytest.fixture(autouse=True)
def seed():
    torch.manual_seed(0)


@pytest.mark.parametrize("dtype", DTYPES)
def test_rms_norm(dtype):
    m = hf.Qwen2RMSNorm(H, eps=1e-6).to(dtype)
    m.weight.data = torch.randn(H).to(dtype)
    x = (torch.randn(1, T, H) * 50).to(dtype)
    assert torch.equal(ops.rms_norm(x, m.weight.data, 1e-6), m(x))


@pytest.mark.parametrize("dtype", DTYPES)
def test_rope_cos_sin(dtype):
    rot = hf.Qwen2RotaryEmbedding(small_config())
    pos = torch.arange(T).unsqueeze(0)
    cos_ref, sin_ref = rot(torch.zeros(1, T, H, dtype=dtype), pos)
    cos, sin = ops.rope_cos_sin(pos, D, THETA, dtype)
    assert cos.dtype == dtype
    assert torch.equal(cos, cos_ref) and torch.equal(sin, sin_ref)


@pytest.mark.parametrize("dtype", DTYPES)
def test_apply_rotary(dtype):
    pos = torch.arange(T).unsqueeze(0)
    cos, sin = ops.rope_cos_sin(pos, D, THETA, dtype)
    q, k = torch.randn(1, HQ, T, D).to(dtype), torch.randn(1, HK, T, D).to(dtype)
    q1, k1 = ops.apply_rotary(q, k, cos, sin)
    q2, k2 = hf.apply_rotary_pos_emb(q, k, cos, sin)
    assert torch.equal(q1, q2) and torch.equal(k1, k2)


def test_repeat_kv():
    x = torch.randn(1, HK, T, D)
    out = ops.repeat_kv(x, HQ // HK)
    assert torch.equal(out, hf.repeat_kv(x, HQ // HK))
    assert torch.equal(out[0, 1], x[0, 0]) and torch.equal(out[0, 2], x[0, 1])     # KV 헤드가 연달아 반복된다


@pytest.mark.parametrize("dtype", DTYPES)
def test_causal_mask(dtype):
    ref = eager_mask(batch_size=1, q_length=T, kv_length=T, dtype=dtype)
    assert torch.equal(ops.causal_mask(T, T, dtype), ref)


@pytest.mark.parametrize("dtype", DTYPES)
def test_attention(dtype):
    q, k, v = (torch.randn(1, n, T, D).to(dtype) for n in (HQ, HK, HK))
    mask = ops.causal_mask(T, T, dtype)
    scores, attn, ctx = ops.causal_softmax_attention(q, k, v, mask, HQ // HK, D**-0.5)
    module = SimpleNamespace(num_key_value_groups=HQ // HK, training=False)
    out_ref, attn_ref = hf.eager_attention_forward(module, q, k, v, mask, scaling=D**-0.5)
    assert torch.equal(attn, attn_ref)
    assert torch.equal(ctx.transpose(1, 2), out_ref)        # HF는 [1,T,Hq,D]로 돌려준다
    assert attn.dtype == dtype and scores.dtype == dtype
    assert torch.all(attn[0, :, 0, 1:] == 0)                 # 첫 토큰은 자기만 본다


@pytest.mark.parametrize("dtype", DTYPES)
def test_silu_mlp(dtype):
    m = hf.Qwen2MLP(small_config()).to(dtype)
    x = torch.randn(1, T, H).to(dtype)
    out, _, _, _ = ops.silu_mlp(x, m.gate_proj.weight, m.up_proj.weight, m.down_proj.weight)
    assert torch.equal(out, m(x))


@pytest.mark.parametrize("dtype", DTYPES)
def test_decoder_layer_small(dtype):
    """작은 무작위 층 전체: 우리 decoder_layer와 HF Qwen2DecoderLayer."""
    cfg = small_config()
    layer = hf.Qwen2DecoderLayer(cfg, 0)
    for p in layer.parameters():
        p.data = torch.randn_like(p) * 0.2
    layer = layer.to(dtype)
    a = layer.self_attn
    w = LayerWeights(ln1=layer.input_layernorm.weight.data, q_w=a.q_proj.weight.data, q_b=a.q_proj.bias.data,
                     k_w=a.k_proj.weight.data, k_b=a.k_proj.bias.data, v_w=a.v_proj.weight.data,
                     v_b=a.v_proj.bias.data, o_w=a.o_proj.weight.data, ln2=layer.post_attention_layernorm.weight.data,
                     gate_w=layer.mlp.gate_proj.weight.data, up_w=layer.mlp.up_proj.weight.data,
                     down_w=layer.mlp.down_proj.weight.data)
    x = torch.randn(1, T, H).to(dtype)
    pos = torch.arange(T).unsqueeze(0)
    cos, sin = ops.rope_cos_sin(pos, D, THETA, dtype)
    mask = ops.causal_mask(T, T, dtype)
    got = {}
    out, k, v = decoder_layer(x, w, ours_config(), cos, sin, mask, 0, lambda n, _l, t: got.__setitem__(n, t))
    ref = layer(x, attention_mask=mask, position_ids=pos, position_embeddings=(cos, sin))
    assert torch.equal(out, ref)
    assert k.shape == (1, HK, T, D) and v.shape == (1, HK, T, D)
    assert got["scores"].shape == (HQ, T, T) and got["mlp.act"].shape == (T, INTER)
