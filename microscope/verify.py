"""V2~V4: 캡처 폴더의 값과 HF eager(bf16)를 비교해 그 폴더에 verification_prefill.json을 쓴다.

사용: python -m microscope.verify --run ~/microscope-runs/<run_id> [--limit-min 40]
우리 엔진은 올리지 않는다(캡처 파일만 읽는다). 메모리에는 HF 모델 하나만 있다.
"""
import argparse
import gc
import json
from pathlib import Path

import torch
from transformers import AutoTokenizer

from . import hf_ref
from .engine import capture, config, guard, model


def _top5(logits: torch.Tensor, decode) -> list[dict]:
    return [{k: r[k] for k in ("id", "text", "logit", "logprob")} for r in model.topk(logits, 5, decode)]


def verify_run(run_dir: Path) -> dict:
    torch.set_num_threads(config.threads())
    man = capture.load_manifest(run_dir)
    ids = man["prompt"]["token_ids"]
    n_layers = man["model"]["layers"]

    def ours(name: str) -> torch.Tensor:
        return torch.from_numpy(capture.read_tensor(run_dir, name, manifest=man))

    hf = hf_ref.load_hf(torch.bfloat16)
    ref = hf_ref.hf_prefill(hf, ids)
    del hf
    gc.collect()
    tok = AutoTokenizer.from_pretrained(str(config.model_dir()))

    def decode(i: int) -> str:
        return tok.decode([i])

    v2 = [{"layer": "embed", **hf_ref.diff(ours("embed"), ref["hidden_states"][0])}]
    v2 += [{"layer": i, **hf_ref.diff(ours(f"L{i}.resid2"), ref["layer_out"][i])} for i in range(n_layers)]
    v2.append({"layer": "final_norm", **hf_ref.diff(ours("final_norm"), ref["hidden_states"][-1])})
    # 참고: hidden_states[l+1](l < 마지막 층)은 forward hook으로 받은 층 출력과 같은 값이어야 한다
    hs_check = all(torch.equal(ref["hidden_states"][i + 1], ref["layer_out"][i]) for i in range(n_layers - 1))

    v3 = [{"layer": i, "k": hf_ref.diff(ours(f"L{i}.k"), ref["keys"][i]),
           "v": hf_ref.diff(ours(f"L{i}.v"), ref["values"][i])} for i in range(n_layers)]
    v4 = [{"layer": i, **hf_ref.diff(ours(f"L{i}.attn"), ref["attentions"][i])} for i in range(n_layers)]

    lb, lf = ours("logits_last_bf16"), ours("logits_last_f32")
    top5 = {"ours_bf16": _top5(lb, decode), "ours_f32": _top5(lf, decode),
            "hf_eager": _top5(ref["logits_last"], decode)}
    res = {
        "run_id": man["run_id"],
        "reference": "transformers Qwen2ForCausalLM, attn_implementation=eager, bf16",
        "threads": torch.get_num_threads(),
        "hidden_states_match_layer_hooks": hs_check,
        "v2_layers": v2,
        "v2_logits_last_bf16_vs_hf": hf_ref.diff(lb, ref["logits_last"]),
        "v2_logits_last_f32_vs_hf": hf_ref.diff(lf, ref["logits_last"]),
        "v2_top5": top5,
        "v2_top1_same": top5["ours_bf16"][0]["id"] == top5["hf_eager"][0]["id"],
        "v2_top5_set_same": {r["id"] for r in top5["ours_bf16"]} == {r["id"] for r in top5["hf_eager"]},
        "v3_kv": v3,
        "v4_attn": v4,
        "memory": {"rss_peak_gib": guard.proc_mem_gib()["VmHWM"]},
    }
    (run_dir / "verification_prefill.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def print_tables(res: dict) -> None:
    print("V2 층별 출력(resid2) vs HF | 층 | 최대 절대 | 평균 절대 | 상대 L2 | 비트 같음")
    for r in res["v2_layers"]:
        print(f"| {r['layer']} | {r['max_abs']:.6g} | {r['mean_abs']:.6g} | {r['rel_l2']:.6g} | {r['bit_equal']} |")
    print("hidden_states[l+1] == 층 hook 출력:", res["hidden_states_match_layer_hooks"])
    print("로짓 bf16 vs HF:", res["v2_logits_last_bf16_vs_hf"], "| f32 재계산 vs HF:", res["v2_logits_last_f32_vs_hf"])
    for k, rows in res["v2_top5"].items():
        print(f"top5 {k}:", [(r["id"], r["text"], round(r["logit"], 4), round(r["logprob"], 4)) for r in rows])
    print("1등 같음:", res["v2_top1_same"], "| 상위 5개 집합 같음:", res["v2_top5_set_same"])
    print("V3 K·V vs HF past_key_values | 층 | K 최대 절대 | K 비트 같음 | V 최대 절대 | V 비트 같음")
    for r in res["v3_kv"]:
        k, v = r["k"], r["v"]
        print(f"| {r['layer']} | {k['max_abs']:.6g} | {k['bit_equal']} | {v['max_abs']:.6g} | {v['bit_equal']} |")
    print("V4 어텐션 가중치 vs HF attentions | 층 | 최대 절대 | 비트 같음")
    for r in res["v4_attn"]:
        print(f"| {r['layer']} | {r['max_abs']:.6g} | {r['bit_equal']} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--limit-min", type=float, default=40)
    a = ap.parse_args()
    guard.start_guard(a.limit_min * 60, label="verify")
    res = verify_run(a.run.expanduser())
    print_tables(res)
    print("저장:", a.run / "verification_prefill.json")


if __name__ == "__main__":
    main()
