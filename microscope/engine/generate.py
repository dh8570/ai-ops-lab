"""생성 반복: Prefill → 첫 토큰 뽑기 → Decode 단계 j = 1, 2, …
(입력 토큰 j, 위치 = 프롬프트 수 + j − 1) → 토큰 j+1 뽑기.
멈춤: 뽑은 토큰이 중단 토큰(eos)이거나 새 토큰 수가 max_new에 닿으면."""
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import torch

from . import model
from .capture import Recorder
from .config import ModelConfig
from .kvcache import KVCache
from .sampler import Sampler, SamplingConfig, argmax_lowest, greedy_record
from .weights import ModelWeights

MAX_NEW_LIMIT = 32
TOP_LOGITS = 50
LOGITS_PATHS = ("f32", "bf16")


@dataclass
class GenerateResult:
    tokens: list[dict]                     # manifest.generated[] (텍스트는 describe가 채움)
    stop_reason: str                       # "eos" | "length"
    recorders: list[Recorder]              # [Prefill, D1, D2, …] (capture=False면 빈 목록)
    phases: list[dict]
    cache: KVCache
    argmax_f32: list[int] = field(default_factory=list)    # 단계마다 float32 로짓의 탐욕 선택(정보용)
    argmax_bf16: list[int] = field(default_factory=list)   # 단계마다 bf16 로짓의 탐욕 선택(정보용)


def _describe_default(i: int) -> tuple[str, str]:
    return "", ""


def generate(prompt_ids: list[int], w: ModelWeights, cfg: ModelConfig, sampling: SamplingConfig, max_new: int,
             eos_ids: list[int], logits_path: str = "f32", dtype: torch.dtype = torch.bfloat16,
             capture: bool = True, describe: Callable[[int], tuple[str, str]] | None = None,
             on_step: Callable[[dict], None] | None = None) -> GenerateResult:
    """describe(id) → (글자, hex). on_step(phase 기록)은 단계가 끝날 때마다 불린다(진행 출력용)."""
    if not 1 <= max_new <= MAX_NEW_LIMIT:
        raise ValueError(f"max_new는 1 이상 {MAX_NEW_LIMIT} 이하여야 한다: {max_new}")
    if logits_path not in LOGITS_PATHS:
        raise ValueError(f"logits_path는 {LOGITS_PATHS} 중 하나: {logits_path}")
    if logits_path == "bf16" and not sampling.greedy:
        raise ValueError("logits_path='bf16'은 검증용 탐욕(temperature 0)에서만 쓴다")
    model.check_total(len(prompt_ids) + max_new)
    describe = describe or _describe_default
    sampler = Sampler(sampling, decode=lambda i: describe(i)[0])
    n_prompt = len(prompt_ids)

    def pick(res, n: int, source: str) -> int:
        lb, lf = res.logits_last, res.logits_last_f32
        out.argmax_bf16.append(argmax_lowest(lb.to(torch.float32)))
        out.argmax_f32.append(argmax_lowest(lf))
        if logits_path == "bf16":                  # 검증용: bf16 로짓의 탐욕(같은 값이면 번호 작은 토큰)
            tid = out.argmax_bf16[-1]
            rec = greedy_record(tid, describe(tid)[0], mode="greedy_bf16")
        else:
            tid, rec = sampler.sample(lf)
        text, hx = describe(tid)
        out.tokens.append({"n": n, "source": source, "id": tid, "text": text, "hex": hx,
                           "argmax": {"f32": out.argmax_f32[-1], "bf16": out.argmax_bf16[-1]},
                           "top50": {"f32": model.topk(lf, TOP_LOGITS, lambda i: describe(i)[0]),
                                     "bf16": model.topk(lb, TOP_LOGITS, lambda i: describe(i)[0])},
                           "sampling": rec})
        return tid

    out = GenerateResult(tokens=[], stop_reason="", recorders=[], phases=[], cache=None)
    rec = Recorder("prefill") if capture else None
    t0 = time.perf_counter()
    res = model.prefill(prompt_ids, w, cfg, dtype, rec)
    out.phases.append({"id": "prefill", "tokens_in": n_prompt, "tokens_total": n_prompt,
                       "seconds": round(time.perf_counter() - t0, 3)})
    if rec is not None:
        out.recorders.append(rec)
    cache = KVCache.from_prefill(res.keys, res.values)
    out.cache = cache
    tid = pick(res, 1, "prefill")
    if on_step:
        on_step({**out.phases[-1], "token": out.tokens[-1]})

    j = 1
    while True:
        if tid in eos_ids:
            out.stop_reason = "eos"
            break
        if len(out.tokens) >= max_new:
            out.stop_reason = "length"
            break
        rec = Recorder("decode", step=j) if capture else None
        t0 = time.perf_counter()
        res = model.decode_step(tid, n_prompt + j - 1, cache, w, cfg, dtype, rec)
        out.phases.append({"id": f"decode:{j}", "step": j, "tokens_in": 1, "tokens_total": n_prompt + j,
                           "seconds": round(time.perf_counter() - t0, 3)})
        if rec is not None:
            out.recorders.append(rec)
        tid = pick(res, j + 1, f"decode:{j}")
        if on_step:
            on_step({**out.phases[-1], "token": out.tokens[-1]})
        j += 1
    return out
