"""기본 입력(또는 --text)으로 Prefill을 돌려 캡처를 ~/microscope-runs/<run_id>/ 에 만든다.

사용: python -m microscope.run [--text "..."] [--out ~/microscope-runs] [--limit-min 30]
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

from .engine import capture, chat, config, guard, model, weights

DEFAULT_OUT = Path.home() / "microscope-runs"
TOPK = 50


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


def run(text: str, out: Path) -> Path:
    torch.set_num_threads(config.threads())
    torch.set_grad_enabled(False)
    cfg = config.load_config()
    tok = chat.tokenizer()
    prompt = chat.build_prompt(text, tok)
    run_id = capture.new_run_id()
    run_dir = out / run_id
    man = build_manifest(run_id, cfg, prompt, tok, "bf16")
    print(f"run_id {run_id} | 토큰 {len(prompt.token_ids)} (앞 틀 {prompt.n_prefix}, 뒤 틀 {prompt.n_suffix}) "
          f"| threads {torch.get_num_threads()} | 메모리 {guard.proc_mem_gib()}", flush=True)

    t0 = time.perf_counter()
    w = weights.load_all(cfg)
    load_s = time.perf_counter() - t0
    print(f"적재 {load_s:.2f}s | 메모리 {guard.proc_mem_gib()}", flush=True)

    rec = capture.Recorder("prefill")
    t0 = time.perf_counter()
    res = model.prefill(prompt.token_ids, w, cfg, torch.bfloat16, rec)
    prefill_s = time.perf_counter() - t0
    print(f"Prefill {prefill_s:.2f}s (캡처 복사 포함) | 메모리 {guard.proc_mem_gib()}", flush=True)

    def decode(i: int) -> str:
        return tok.decode([i])

    man["phases"] = [{"id": "prefill", "tokens_in": len(prompt.token_ids), "tokens_total": len(prompt.token_ids),
                      "seconds": round(prefill_s, 3)}]
    man["topk"] = {"bf16_path": model.topk(res.logits_last, TOPK, decode),
                   "float32_path": model.topk(res.logits_last_f32, TOPK, decode)}
    man["timing"] = {"load_s": round(load_s, 3), "prefill_s": round(prefill_s, 3)}
    del res, w
    gc.collect()
    man["memory"] = {"rss_peak_gib": guard.proc_mem_gib()["VmHWM"]}
    wr = rec.write(run_dir, man)
    print(f"캡처 쓰기 {wr['seconds']:.2f}s | prefill.bin {wr['bin_bytes']} bytes "
          f"({wr['bin_bytes'] / 2**20:.1f} MiB) | 텐서 {len(rec.tensors)}개", flush=True)
    print("상위 5개(bf16 경로):", json.dumps(man["topk"]["bf16_path"][:5], ensure_ascii=False), flush=True)
    print("상위 5개(float32 재계산):", json.dumps(man["topk"]["float32_path"][:5], ensure_ascii=False), flush=True)
    print(f"저장: {run_dir}", flush=True)
    return run_dir


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=chat.DEFAULT_USER_TEXT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit-min", type=float, default=30)
    a = ap.parse_args()
    guard.start_guard(a.limit_min * 60, label="run")
    run(a.text, a.out)


if __name__ == "__main__":
    main()
