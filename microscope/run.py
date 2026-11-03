"""기본 입력(또는 --text)으로 Prefill과 Decode를 돌려 캡처를 ~/microscope-runs/<run_id>/ 에 만든다.

사용: python -m microscope.run [--text "..."] [--out ~/microscope-runs] [--limit-min 30]
        [--max-new 8] [--temperature 0] [--top-k K] [--top-p P] [--seed S] [--logits-path f32|bf16]
환경 변수: MICROSCOPE_THREADS(기본 6), MICROSCOPE_MODEL_DIR(기본 ~/microscope-spike/model)
"""
import argparse
import gc
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import torch
import transformers

from .engine import capture, chat, config, generate, guard, weights
from .engine.sampler import SamplingConfig

DEFAULT_OUT = Path.home() / "microscope-runs"


def build_manifest(run_id: str, cfg: config.ModelConfig, prompt: chat.Prompt, tok, dtype_name: str) -> dict:
    return {
        "schema_version": capture.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "microscope",
        "evidence": "microscope",
        "model": {"repo": config.DEFAULT_REPO, "revision": config.revision(), "layers": cfg.layers,
                  "hidden_size": cfg.hidden_size, "heads": cfg.heads, "kv_heads": cfg.kv_heads,
                  "head_dim": cfg.head_dim, "intermediate_size": cfg.intermediate_size, "vocab_size": cfg.vocab_size},
        "environment": {"torch": torch.__version__, "transformers": transformers.__version__,
                        "threads": torch.get_num_threads(), "dtype": dtype_name,
                        "cpu_capability": torch.backends.cpu.get_cpu_capability()},
        "prompt": {"user_text": prompt.user_text, "template_string": prompt.template_string,
                   "token_ids": prompt.token_ids, "tokens": chat.token_table(tok, prompt.token_ids),
                   "n_prefix": prompt.n_prefix, "n_suffix": prompt.n_suffix},
    }


def run(text: str, out: Path, sampling: SamplingConfig | None = None, max_new: int = 8,
        logits_path: str = "f32") -> Path:
    sampling = sampling or SamplingConfig()
    torch.set_num_threads(config.threads())
    torch.set_grad_enabled(False)
    cfg = config.load_config()
    eos = config.eos_token_ids()
    tok = chat.tokenizer()
    prompt = chat.build_prompt(text, tok)
    run_id = capture.new_run_id()
    run_dir = out / run_id
    man = build_manifest(run_id, cfg, prompt, tok, "bf16")
    print(f"run_id {run_id} | 토큰 {len(prompt.token_ids)} (앞 틀 {prompt.n_prefix}, 뒤 틀 {prompt.n_suffix}) "
          f"| threads {torch.get_num_threads()} | 뽑기 {sampling.as_dict()} | logits_path {logits_path} "
          f"| 메모리 {guard.proc_mem_gib()}", flush=True)

    t0 = time.perf_counter()
    w = weights.load_all(cfg)
    load_s = time.perf_counter() - t0
    print(f"적재 {load_s:.2f}s | 메모리 {guard.proc_mem_gib()}", flush=True)

    def describe(i: int) -> tuple[str, str]:
        return tok.decode([i]), chat.token_bytes(tok, i).hex()

    def on_step(p: dict) -> None:
        t = p["token"]
        s = t["sampling"]
        extra = "" if s["u"] is None else f" | u {s['u']:.6f} 후보 {s['kept_after_top_k']}→{s['kept_after_top_p']}"
        print(f"{p['id']}: {p['seconds']:.3f}s (캡처 복사 포함) → 토큰 {t['n']} id {t['id']} {t['text']!r}{extra} "
              f"| 메모리 {guard.proc_mem_gib()}", flush=True)

    res = generate.generate(prompt.token_ids, w, cfg, sampling, max_new, eos, logits_path, torch.bfloat16,
                            capture=True, describe=describe, on_step=on_step)

    def decode(i: int) -> str:
        return tok.decode([i])

    first = res.tokens[0]["top50"]
    man["phases"] = res.phases
    man["topk"] = {"bf16_path": first["bf16"], "float32_path": first["f32"]}           # Prefill 끝(M2-a와 같은 필드)
    man["generation"] = {"max_new": max_new, "stop_reason": res.stop_reason, "logits_path": logits_path,
                         "eos_token_ids": eos, "sampling": sampling.as_dict()}
    man["generated"] = res.tokens
    man["generated_text"] = tok.decode([t["id"] for t in res.tokens])
    decode_s = sum(p["seconds"] for p in res.phases[1:])
    man["timing"] = {"load_s": round(load_s, 3), "prefill_s": res.phases[0]["seconds"], "decode_s": round(decode_s, 3)}
    recorders = res.recorders
    del res, w
    gc.collect()
    man["memory"] = {"rss_peak_gib": guard.proc_mem_gib()["VmHWM"]}
    wr = capture.write_run(run_dir, man, recorders)
    n_t = sum(len(r.tensors) for r in recorders)
    sizes = " | ".join(f"{k} {v} bytes ({v / 2**20:.1f} MiB)" for k, v in wr["bytes"].items())
    print(f"캡처 쓰기 {wr['seconds']:.2f}s | {sizes} | 텐서 {n_t}개", flush=True)
    print(f"생성: {man['generated_text']!r} | 멈춤 {man['generation']['stop_reason']} | Decode 합계 {decode_s:.2f}s",
          flush=True)
    print("Prefill 끝 상위 5개(bf16 경로):", json.dumps(man["topk"]["bf16_path"][:5], ensure_ascii=False), flush=True)
    print("Prefill 끝 상위 5개(float32 재계산):", json.dumps(man["topk"]["float32_path"][:5], ensure_ascii=False),
          flush=True)
    print(f"저장: {run_dir}", flush=True)
    return run_dir


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=chat.DEFAULT_USER_TEXT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit-min", type=float, default=30)
    ap.add_argument("--max-new", type=int, default=8, help=f"새 토큰 수(1..{generate.MAX_NEW_LIMIT})")
    ap.add_argument("--temperature", type=float, default=0.0, help="0이면 탐욕")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None, help="temperature > 0이면 필수")
    ap.add_argument("--logits-path", choices=generate.LOGITS_PATHS, default="f32")
    a = ap.parse_args()
    try:
        sampling = SamplingConfig(temperature=a.temperature, top_k=a.top_k, top_p=a.top_p, seed=a.seed)
    except ValueError as e:
        ap.error(str(e))
    if not 1 <= a.max_new <= generate.MAX_NEW_LIMIT:
        ap.error(f"--max-new는 1..{generate.MAX_NEW_LIMIT}")
    guard.start_guard(a.limit_min * 60, label="run")
    run(a.text, a.out, sampling, a.max_new, a.logits_path)


if __name__ == "__main__":
    main()
