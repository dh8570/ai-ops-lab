"""V1: 층 하나를 float32로 우리 계산과 HF Qwen2DecoderLayer에 같은 입력으로 넣어 비교한다.

입력 = HF bf16 모델의 hidden_states[l]을 float32로 바꾼 값. 가중치 = 같은 층의 가중치를 float32로 바꾼 값(양쪽 모두).
하위 단계(정규화, QKV, RoPE, 어텐션, 출력 투영, MLP)별 차이도 함께 기록해 처음 벌어지는 곳을 찾는다.
사용: python -m microscope.verify_layer [--layers 0,14,27] [--out 파일.json]
"""
import argparse
import copy
import gc
import json
from pathlib import Path

import torch
from transformers.masking_utils import create_causal_mask
from transformers.models.qwen2.modeling_qwen2 import apply_rotary_pos_emb

from . import hf_ref
from .engine import chat, config, guard, ops, weights
from .engine.layer import decoder_layer

TOL = 1e-4
STAGES = ("norm1", "q_pre", "k_pre", "v", "q", "k", "attn", "ctx", "attn_out", "resid1", "norm2", "mlp_out", "resid2")


@torch.no_grad()
def prepare(layers: list[int], ids: list[int]) -> dict:
    """HF bf16에서 층 입력과 float32 층 복사본, 공식 cos·sin·마스크(float32)를 얻고 HF 모델은 해제한다."""
    hf = hf_ref.load_hf(torch.bfloat16)
    out = hf(input_ids=torch.tensor([ids]), use_cache=False, output_hidden_states=True)
    xs = {i: out.hidden_states[i].to(torch.float32).clone() for i in layers}
    hf_layers = {i: copy.deepcopy(hf.model.layers[i]).to(torch.float32) for i in layers}
    pos = torch.arange(len(ids)).unsqueeze(0)
    x0 = xs[layers[0]]
    cos, sin = hf.model.rotary_emb(x0, pos)                      # x가 float32이므로 float32 cos·sin
    mask = create_causal_mask(config=hf.config, inputs_embeds=x0, attention_mask=None, past_key_values=None,
                              position_ids=pos)
    del hf, out
    gc.collect()
    return {"xs": xs, "hf_layers": hf_layers, "cos": cos, "sin": sin, "mask": mask, "pos": pos}


@torch.no_grad()
def run_hf_layer(layer, x, cos, sin, mask, pos) -> dict:
    """HF 층을 hook으로 열어 하위 단계 값을 모은다(배치 차원 제거, 우리 캡처와 같은 모양)."""
    att = layer.self_attn
    t, d = x.shape[1], att.head_dim
    got: dict[str, torch.Tensor] = {}

    def heads(o):
        return o.view(1, t, -1, d).transpose(1, 2)[0]

    hs = [
        layer.input_layernorm.register_forward_hook(lambda _m, _i, o: got.__setitem__("norm1", o[0])),
        att.q_proj.register_forward_hook(lambda _m, _i, o: got.__setitem__("q_pre", heads(o))),
        att.k_proj.register_forward_hook(lambda _m, _i, o: got.__setitem__("k_pre", heads(o))),
        att.v_proj.register_forward_hook(lambda _m, _i, o: got.__setitem__("v", heads(o))),
        att.register_forward_hook(lambda _m, _i, o: got.__setitem__("attn", o[1][0])),
        att.o_proj.register_forward_pre_hook(lambda _m, i: got.__setitem__("ctx", heads(i[0]))),
        att.o_proj.register_forward_hook(lambda _m, _i, o: got.__setitem__("attn_out", o[0])),
        layer.post_attention_layernorm.register_forward_hook(lambda _m, _i, o: got.__setitem__("norm2", o[0])),
        layer.mlp.register_forward_hook(lambda _m, _i, o: got.__setitem__("mlp_out", o[0])),
    ]
    try:
        y = layer(x, attention_mask=mask, position_ids=pos, position_embeddings=(cos, sin))
    finally:
        for h in hs:
            h.remove()
    q, k = apply_rotary_pos_emb(got["q_pre"][None], got["k_pre"][None], cos, sin)
    got["q"], got["k"] = q[0], k[0]
    got["resid1"] = x[0] + got["attn_out"]
    got["resid2"] = (y[0] if isinstance(y, tuple) else y)[0]
    return got


@torch.no_grad()
def run_our_layer(x, w, cfg, cos, sin, mask, layer: int) -> dict:
    got: dict[str, torch.Tensor] = {}
    decoder_layer(x, w, cfg, cos, sin, mask, layer, lambda n, _l, t: got.__setitem__(n, t.clone()))
    return got


def verify_layers(layers: list[int]) -> dict:
    torch.set_num_threads(config.threads())
    cfg = config.load_config()
    ids = chat.build_prompt().token_ids
    p = prepare(layers, ids)
    t = len(ids)
    cos, sin = ops.rope_cos_sin(p["pos"], cfg.head_dim, cfg.rope_theta, torch.float32)
    mask = ops.causal_mask(t, t, torch.float32)
    res = {"tolerance": TOL, "threads": torch.get_num_threads(),
           "cos_sin_equal_hf": bool(torch.equal(cos, p["cos"]) and torch.equal(sin, p["sin"])),
           "mask_equal_hf": bool(torch.equal(mask, p["mask"])), "layers": []}
    for i in layers:
        ref = run_hf_layer(p["hf_layers"][i], p["xs"][i], p["cos"], p["sin"], p["mask"], p["pos"])
        w = weights.load_layer(i, cfg).to(torch.float32)
        ours = run_our_layer(p["xs"][i], w, cfg, cos, sin, mask, i)
        stages = {s: hf_ref.diff(ours[s], ref[s]) for s in STAGES}
        first = next((s for s in STAGES if stages[s]["max_abs"] > TOL), None)
        res["layers"].append({"layer": i, "output": stages["resid2"], "pass": stages["resid2"]["max_abs"] <= TOL,
                              "first_stage_over_tol": first, "stages": stages,
                              "input_absmax": float(p["xs"][i].abs().max())})
        del w, ours, ref
        gc.collect()
    return res


def print_table(res: dict) -> None:
    print(f"cos·sin = HF: {res['cos_sin_equal_hf']} | 마스크 = HF: {res['mask_equal_hf']} | 기준 {res['tolerance']}")
    print("| 층 | 출력 최대 절대 | 평균 절대 | 상대 L2 | 비트 같음 | 합격 | 처음 넘는 단계 | 입력 |max| |")
    for r in res["layers"]:
        o = r["output"]
        print(f"| {r['layer']} | {o['max_abs']:.6g} | {o['mean_abs']:.6g} | {o['rel_l2']:.6g} | {o['bit_equal']} "
              f"| {r['pass']} | {r['first_stage_over_tol']} | {r['input_absmax']:.6g} |")
    print("하위 단계별 최대 절대 차이(비트 같으면 =)")
    print("| 층 | " + " | ".join(STAGES) + " |")
    for r in res["layers"]:
        cells = [("=" if r["stages"][s]["bit_equal"] else f"{r['stages'][s]['max_abs']:.3g}") for s in STAGES]
        print(f"| {r['layer']} | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default=None, help="쉼표로 구분. 기본: 0, 중간(층 수//2), 마지막")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--limit-min", type=float, default=40)
    a = ap.parse_args()
    guard.start_guard(a.limit_min * 60, label="verify_layer")
    n = config.load_config().layers
    layers = [int(x) for x in a.layers.split(",")] if a.layers else [0, n // 2, n - 1]
    res = verify_layers(layers)
    print_table(res)
    if a.out:
        a.out.write_text(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
