"""V8~V10: 캡처 폴더의 Decode 값과 HF eager(bf16)를 비교한다. verify.py가 부른다.

HF 반복: Prefill 뒤 단계 j마다 우리가 만든 토큰 j를 past_key_values와 함께 넣는다(같은 토큰열로 따라가기).
단계마다 HF가 고를 탐욕 토큰도 기록해, 우리 토큰열과 같으면 HF가 스스로 이어 쓴 결과와 같다는 뜻이 된다.
우리 엔진은 올리지 않는다(캡처 파일만 읽는다).
"""
import torch

from . import hf_ref
from .engine import capture
from .engine.sampler import argmax_lowest


def _max_over(diffs: list[dict]) -> dict:
    return {"max_abs": max(d["max_abs"] for d in diffs), "bit_equal": all(d["bit_equal"] for d in diffs)}


@torch.no_grad()
def hf_loop_compare(hf, run_dir, man: dict) -> dict:
    """V8(a)·V9·V10. HF 반복을 돌리며 단계마다 우리 캡처와 비교한다."""
    ids = man["prompt"]["token_ids"]
    toks = [g["id"] for g in man["generated"]]
    n_layers = man["model"]["layers"]
    n_steps = sum(1 for p in man["phases"] if p["id"].startswith("decode:"))

    def ours(name: str) -> torch.Tensor:
        return torch.from_numpy(capture.read_tensor(run_dir, name, manifest=man))

    layer_out: dict[int, torch.Tensor] = {}
    norm: dict[str, torch.Tensor] = {}
    hooks = [layer.register_forward_hook(lambda _m, _i, o, i=i: layer_out.__setitem__(i, o[0].clone()))
             for i, layer in enumerate(hf.model.layers)]
    hooks.append(hf.model.norm.register_forward_hook(lambda _m, _i, o: norm.__setitem__("h", o[0].clone())))
    steps, attn_rows, hf_greedy = [], [], []
    try:
        out = hf(input_ids=torch.tensor([ids]), use_cache=True)
        pkv = out.past_key_values
        hf_greedy.append(argmax_lowest(out.logits[0, -1].float()))
        for j in range(1, n_steps + 1):
            out = hf(input_ids=torch.tensor([[toks[j - 1]]]), past_key_values=pkv, use_cache=True,
                     output_attentions=True)
            pkv = out.past_key_values
            p = f"D{j}."
            resid = [hf_ref.diff(ours(f"{p}L{i}.resid2"), layer_out[i]) for i in range(n_layers)]
            ks = [hf_ref.diff(ours(f"{p}L{i}.k"), pkv.layers[i].keys[0, :, -1:, :]) for i in range(n_layers)]
            vs = [hf_ref.diff(ours(f"{p}L{i}.v"), pkv.layers[i].values[0, :, -1:, :]) for i in range(n_layers)]
            at = [hf_ref.diff(ours(f"{p}L{i}.attn"), out.attentions[i][0]) for i in range(n_layers)]
            lg = out.logits[0, -1]
            hf_greedy.append(argmax_lowest(lg.float()))
            steps.append({"step": j, "token_in": toks[j - 1], "position": len(ids) + j - 1,
                          "tk": int(out.attentions[0].shape[-1]),
                          "resid2": _max_over(resid), "k": _max_over(ks), "v": _max_over(vs),
                          "final_norm": hf_ref.diff(ours(f"{p}final_norm"), norm["h"]),
                          "logits_bf16": hf_ref.diff(ours(f"{p}logits_last_bf16"), lg),
                          "logits_f32_vs_hf_bf16": hf_ref.diff(ours(f"{p}logits_last_f32"), lg),
                          "resid2_by_layer": [d["max_abs"] for d in resid]})
            attn_rows.append({"step": j, "by_layer": [d["max_abs"] for d in at],
                              "bit_equal": all(d["bit_equal"] for d in at)})
    finally:
        for h in hooks:
            h.remove()
    cache = []
    for i in range(n_layers):
        k, v = capture.read_kv_cache(run_dir, i, n_steps, manifest=man)
        cache.append({"layer": i, "k": hf_ref.diff(k, pkv.layers[i].keys[0]),
                      "v": hf_ref.diff(v, pkv.layers[i].values[0]), "length": int(pkv.layers[i].keys.shape[-2])})
    return {"n_decode_steps": n_steps, "ours_tokens": toks, "hf_greedy_tokens": hf_greedy,
            "v8a_same": hf_greedy == toks, "v9_steps": steps, "v9_cache": cache, "v10_attn": attn_rows}


@torch.no_grad()
def hf_generate_compare(hf, man: dict) -> dict:
    """V8(b): HF generate 탐욕. repetition_penalty를 1.0으로 덮어쓰고, 처리기가 로짓을 바꾸지 않았는지
    output_logits(가공 전)와 output_scores(가공 후)가 단계마다 같은지로 확인한다.
    정보로 모델 기본값(repetition_penalty 1.05 등)을 그대로 둔 탐욕도 돌린다."""
    ids = torch.tensor([man["prompt"]["token_ids"]])
    n = man["generation"]["max_new"]
    common = {"attention_mask": torch.ones_like(ids), "max_new_tokens": n, "return_dict_in_generate": True,
              "output_scores": True, "output_logits": True}
    g = hf.generate(ids, do_sample=False, repetition_penalty=1.0, **common)
    toks = g.sequences[0, ids.shape[1]:].tolist()
    untouched = all(torch.equal(s, lg) for s, lg in zip(g.scores, g.logits, strict=True))
    g2 = hf.generate(ids, do_sample=False, **common)
    toks2 = g2.sequences[0, ids.shape[1]:].tolist()
    return {"v8b_tokens": toks, "v8b_same": toks == [x["id"] for x in man["generated"]],
            "v8b_scores_equal_raw_logits": untouched,
            "info_default_generation_config_tokens": toks2,
            "info_default_scores_equal_raw_logits": all(torch.equal(s, lg)
                                                         for s, lg in zip(g2.scores, g2.logits, strict=True))}


def f32_vs_bf16_greedy(man: dict) -> list[dict]:
    """정보: 단계마다 float32 로짓의 탐욕 선택과 bf16 로짓의 탐욕 선택(같은 값이면 번호 작은 토큰)이 다른 곳."""
    return [{"n": t["n"], "source": t["source"], **t["argmax"]}
            for t in man["generated"] if t["argmax"]["f32"] != t["argmax"]["bf16"]]


def all_bit_equal(dec: dict) -> bool:
    """V9·V10 전부(단계별 resid2·k·v·final_norm·bf16 로짓, 마지막 캐시, 어텐션)가 비트 단위로 같은가."""
    steps = all(s[k]["bit_equal"] for s in dec["v9_steps"] for k in ("resid2", "k", "v", "final_norm", "logits_bf16"))
    cache = all(c["k"]["bit_equal"] and c["v"]["bit_equal"] for c in dec["v9_cache"])
    return steps and cache and all(r["bit_equal"] for r in dec["v10_attn"])


def print_tables(dec: dict) -> None:
    print(f"V8 우리 토큰 {dec['ours_tokens']}")
    print(f"V8(a) HF 반복 탐욕 {dec['hf_greedy_tokens']} 같음 {dec['v8a_same']}")
    print(f"V8(b) HF generate {dec['v8b_tokens']} 같음 {dec['v8b_same']} | 처리기가 로짓을 안 바꿈 "
          f"{dec['v8b_scores_equal_raw_logits']}")
    print(f"(정보) generation_config 기본값 탐욕 {dec['info_default_generation_config_tokens']} | 로짓 안 바꿈 "
          f"{dec['info_default_scores_equal_raw_logits']}")
    print(f"(정보) f32·bf16 탐욕이 다른 단계: {dec['info_f32_vs_bf16_greedy_differs']}")
    print("V9 단계별 vs HF | 단계 | 입력 토큰 | 위치 | Tk | resid2 최대(28층) | k 최대 | v 최대 | final_norm "
          "| 로짓 bf16 | 비트 같음(전부) | (정보) f32 로짓 vs HF bf16 최대")
    for s in dec["v9_steps"]:
        eq = all(s[k]["bit_equal"] for k in ("resid2", "k", "v", "final_norm", "logits_bf16"))
        print(f"| D{s['step']} | {s['token_in']} | {s['position']} | {s['tk']} | {s['resid2']['max_abs']:.6g} "
              f"| {s['k']['max_abs']:.6g} | {s['v']['max_abs']:.6g} | {s['final_norm']['max_abs']:.6g} "
              f"| {s['logits_bf16']['max_abs']:.6g} | {eq} | {s['logits_f32_vs_hf_bf16']['max_abs']:.6g} |")
    print(f"V9 마지막 KV 캐시 vs HF DynamicCache (단계 {dec['n_decode_steps']} 뒤) "
          "| 층 | 길이 | K 최대 | K 비트 | V 최대 | V 비트")
    for c in dec["v9_cache"]:
        print(f"| {c['layer']} | {c['length']} | {c['k']['max_abs']:.6g} | {c['k']['bit_equal']} "
              f"| {c['v']['max_abs']:.6g} | {c['v']['bit_equal']} |")
    n = len(dec["v10_attn"][0]["by_layer"]) if dec["v10_attn"] else 0
    print("V10 Decode 어텐션 vs HF attentions, 층별 최대 절대 차이 | 층 | " +
          " | ".join(f"D{r['step']}" for r in dec["v10_attn"]) + " |")
    for i in range(n):
        print(f"| {i} | " + " | ".join(f"{r['by_layer'][i]:.6g}" for r in dec["v10_attn"]) + " |")
    print("V10 단계별 비트 같음:", [r["bit_equal"] for r in dec["v10_attn"]])
    print("V9·V10 전부 비트 같음:", all_bit_equal(dec))
