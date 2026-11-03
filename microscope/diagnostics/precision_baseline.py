"""정밀도 진단: 같은 프롬프트·같은 강제 토큰열에서 다음 토큰의 로그확률을 네 방식으로 잰다. 숫자만 보고하고 단언은 없다.

 (1) 현미경 bf16: 캡처(manifest)의 값(HF eager와 비트 일치). 상위 50개만 있다.
 (2) 몸통 float32(기준선): 엔진의 float32 모드   (3) HF sdpa bf16   (4) Ollama Q4_K_M(raw 요청, 상위 20개)
입력: ~/microscope-runs/r-20261029-072238-7ed8/manifest.json의 prompt.token_ids(72개)와 generated[].id 8개(강제 토큰열).
위치 n(1..8)의 확률 = 앞 n-1개 강제 토큰 뒤에 올 토큰의 float32 log_softmax(temperature 1, 벌점 없음) 로그확률.

실행(레포 밖 가상환경을 쓴다):
  PYTHONDONTWRITEBYTECODE=1 $HOME/microscope-spike/venv/bin/python -m microscope.diagnostics.precision_baseline
결과: ~/microscope-runs/diagnostics/precision_baseline.json. 환경 변수 MICROSCOPE_THREADS(기본 6).
예상 소요·메모리(2026-10-30 실측): 약 75초(Ollama 16회 약 3초, float32 몸통 약 60초, HF sdpa 약 17초),
RSS 최대 약 17.6GiB(float32 몸통의 Prefill, 층 하나분 임시 float32 복사 포함).
MemAvailable은 실측에서 27.9GiB 아래로 내려가지 않았다.
한 번에 모델 하나만 올린다(순서대로 del·gc). MemAvailable < 8GiB 또는 40분 초과면 즉시 중단.
"""
import gc
import json
import math
import time
import urllib.request
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM

from microscope.engine import chat, config, guard, model, weights
from microscope.engine.kvcache import KVCache

RUN_DIR = Path.home() / "microscope-runs" / "r-20261029-072238-7ed8"
OUT_PATH = Path.home() / "microscope-runs" / "diagnostics" / "precision_baseline.json"
OLLAMA = "http://10.97.0.1:11434"
EXPECTED = [26698, 79004, 56983, 43590, 24897, 85403, 129704, 42039]
NPOS = len(EXPECTED)
NAMES = {"f32": "몸통 float32", "mic": "현미경 bf16", "sdpa": "HF sdpa", "ollama": "Ollama Q4"}
OUT_OF_LIST = "목록 밖"


def log(*a) -> None:
    print(*a, flush=True)


def logprobs(logits: torch.Tensor) -> torch.Tensor:
    return torch.log_softmax(logits.to(torch.float32), dim=-1)


def step_log(label: str, t0: float) -> float:
    s = time.perf_counter() - t0
    log(f"  {label}: {s:.2f}s | 메모리 {guard.proc_mem_gib()}")
    return round(s, 3)


def run_f32_body(ids: list[int], forced: list[int]) -> tuple[list[torch.Tensor], dict]:
    """(2) 엔진의 float32 모드로 강제 토큰열을 따라간다. generate()는 강제 토큰열을 못 받아 쓰지 않는다."""
    cfg = config.load_config()
    t0 = time.perf_counter()
    w = weights.load_all(cfg)
    secs = {"load": step_log("float32 몸통: 가중치 적재", t0)}
    t0 = time.perf_counter()
    res = model.prefill(ids, w, cfg, torch.float32)
    out = [logprobs(res.logits_last)]
    secs["pos1_prefill"] = step_log("위치 1 (Prefill)", t0)
    cache = KVCache.from_prefill(res.keys, res.values)
    del res
    for n in range(2, NPOS + 1):
        t0 = time.perf_counter()
        r = model.decode_step(forced[n - 2], len(ids) + n - 2, cache, w, cfg, torch.float32)
        out.append(logprobs(r.logits_last))
        secs[f"pos{n}_decode"] = step_log(f"위치 {n} (입력 토큰 {forced[n - 2]}, 위치 {len(ids) + n - 2})", t0)
    del w, cache
    gc.collect()
    return out, secs


def run_hf_sdpa(ids: list[int], forced: list[int]) -> tuple[list[torch.Tensor], dict]:
    """(3) HF sdpa bf16. Prefill 뒤 past_key_values로 강제 토큰열을 따라간다(hf_ref.py는 eager 전용이라 쓰지 않는다)."""
    t0 = time.perf_counter()
    hf = AutoModelForCausalLM.from_pretrained(str(config.model_dir()), dtype=torch.bfloat16,
                                              attn_implementation="sdpa").eval()
    secs = {"load": step_log(f"HF sdpa 적재 (attn={hf.config._attn_implementation})", t0)}
    out = []
    with torch.no_grad():
        t0 = time.perf_counter()
        o = hf(input_ids=torch.tensor([ids]), use_cache=True)
        pkv = o.past_key_values
        out.append(logprobs(o.logits[0, -1]))
        secs["pos1_prefill"] = step_log("위치 1 (Prefill)", t0)
        for n in range(2, NPOS + 1):
            t0 = time.perf_counter()
            o = hf(input_ids=torch.tensor([[forced[n - 2]]]), past_key_values=pkv, use_cache=True)
            pkv = o.past_key_values
            out.append(logprobs(o.logits[0, -1]))
            secs[f"pos{n}_decode"] = step_log(f"위치 {n}", t0)
    del hf, o, pkv
    gc.collect()
    return out, secs


def ollama_call(prompt: str) -> dict:
    body = {"model": "qwen2.5:7b", "prompt": prompt, "raw": True, "stream": False, "logprobs": True,
            "top_logprobs": 20, "options": {"temperature": 0, "seed": 1, "repeat_penalty": 1.0, "num_predict": 1,
                                            "num_ctx": 4096}}
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    r = json.load(urllib.request.urlopen(req, timeout=300))
    top = ((r.get("logprobs") or [{}])[0]).get("top_logprobs", [])
    return {"seconds": round(time.perf_counter() - t, 3), "prompt_eval_count": r.get("prompt_eval_count"),
            "prompt_eval_cached_count": r.get("prompt_eval_cached_count"), "eval_count": r.get("eval_count"),
            "response": r.get("response"), "top": [{"token": e.get("token"), "bytes": e.get("bytes"),
                                                    "logprob": e["logprob"]} for e in top]}


def run_ollama(tok, man: dict, n_prompt: int) -> list[dict]:
    """(4) 위치 n마다 두 번 부른다(같은 요청을 연달아). 경계 불일치·맞춤 실패 위치는 excluded에 이유를 적는다."""
    out = []
    for n in range(1, NPOS + 1):
        prompt = man["prompt"]["template_string"] + "".join(g["text"] for g in man["generated"][: n - 1])
        calls = [ollama_call(prompt), ollama_call(prompt)]
        expect = n_prompt + n - 1
        c = calls[1]
        cnt, cached = c["prompt_eval_count"], c["prompt_eval_cached_count"]
        ok = cnt == expect or (cached is not None and cnt is not None and cnt + cached == expect)
        local = len(chat.encode(tok, prompt))
        excluded = None if ok else f"경계 불일치(prompt_eval_count {cnt}, cached {cached}, 기대 {expect})"
        if excluded is None and any(e["bytes"] is None for call in calls for e in call["top"]):
            excluded = "맞춤 실패(bytes 없음)"
        out.append({"n": n, "prompt_tail": prompt[-40:], "expected_prompt_tokens": expect, "local_token_count": local,
                    "calls": calls, "excluded": excluded})
        c0 = calls[0]
        log(f"  위치 {n}: 호출 {c0['seconds']}s/{c['seconds']}s | prompt_eval_count {c0['prompt_eval_count']}/{cnt}"
            f" (cached {c0['prompt_eval_cached_count']}/{cached}) 기대 {expect} 로컬토큰 {local}"
            f" | 1등 {c['response']!r} | {excluded or 'OK'}")
    return out


class Data:
    """네 방식의 로그확률을 (방식, 위치, 토큰 번호)로 꺼내 쓰는 도우미. 값이 없으면(목록 밖·제외) None."""

    def __init__(self, tok, man, lp32, lpsdpa, olla):
        self.tok, self.man, self.lp = tok, man, {"f32": lp32, "sdpa": lpsdpa}
        self.olla = olla
        self.bytes_cache: dict[int, tuple] = {}
        self.mic = [{r["id"]: r["logprob"] for r in g["top50"]["bf16"]} for g in man["generated"]]
        self.mic_f32head = [{r["id"]: r["logprob"] for r in g["top50"]["f32"]} for g in man["generated"]]
        self.olla_by_call = [[{tuple(e["bytes"]): e["logprob"] for e in c["top"] if e["bytes"] is not None}
                              for c in o["calls"]] for o in olla]

    def tb(self, i: int) -> tuple:
        if i not in self.bytes_cache:
            self.bytes_cache[i] = tuple(chat.token_bytes(self.tok, i))
        return self.bytes_cache[i]

    def get(self, method: str, n: int, tid: int, call: int = 1):
        k = n - 1
        if method in self.lp:
            return float(self.lp[method][k][tid])
        if method == "mic":
            return self.mic[k].get(tid)
        if self.olla[k]["excluded"] is not None:
            return None
        return self.olla_by_call[k][call].get(self.tb(tid))

    def top1(self, method: str, n: int):
        """1등 토큰의 바이트(방식끼리 비교용)와 확률. 사용할 수 없으면 None."""
        k = n - 1
        if method in self.lp:
            tid = int(self.lp[method][k].argmax())
            return self.tb(tid), float(self.lp[method][k][tid]), tid
        if method == "mic":
            tid = self.man["generated"][k]["argmax"]["bf16"]
            return self.tb(tid), self.mic[k][tid], tid
        if self.olla[k]["excluded"] is not None:
            return None
        b, lpv = max(self.olla_by_call[k][1].items(), key=lambda kv: kv[1])
        return b, lpv, None


def compare_tokens(d: Data, n: int) -> list[int]:
    """비교용 토큰 = float32 기준선의 상위 5개 + 실제 강제 토큰(이미 있으면 추가하지 않음)."""
    top5 = d.lp["f32"][n - 1].topk(5).indices.tolist()
    forced = d.man["generated"][n - 1]["id"]
    return top5 + ([forced] if forced not in top5 else [])


def agg(v: list[float]):
    if not v:
        return None
    return {"n": len(v), "mean_abs": sum(map(abs, v)) / len(v), "max_abs": max(map(abs, v)), "mean": sum(v) / len(v)}


def diffs(d: Data, a: str, b: str, positions) -> tuple[list[float], int]:
    """float32 상위 5개 토큰에서 (a - b) 로그확률 차이 목록과 한쪽이라도 값이 없어 빠진 개수."""
    out, missing = [], 0
    for n in positions:
        for t in compare_tokens(d, n)[:5]:
            va, vb = d.get(a, n, t), d.get(b, n, t)
            if va is None or vb is None:
                missing += 1
            else:
                out.append(va - vb)
    return out, missing


def top20(tok, lp: torch.Tensor) -> list:
    return [(i, tok.decode([i]), float(lp[i])) for i in lp.topk(20).indices.tolist()]


def fmt(x) -> str:
    return OUT_OF_LIST if x is None else f"{x:.4f}"


def top1_cell(d: Data, m: str, n: int) -> str:
    t = d.top1(m, n)
    if t is None:
        return "제외: " + d.olla[n - 1]["excluded"].split("(")[0]
    text = d.tok.decode([t[2]]) if t[2] is not None else bytes(t[0]).decode("utf-8", "replace")
    return f"{text!r} {100 * math.exp(t[1]):.2f}%"


def table_a(d: Data) -> list[str]:
    rows = ["| 위치 | 강제 토큰 | " + " | ".join(NAMES.values()) + " |", "|" + " --- |" * (2 + len(NAMES))]
    for n in range(1, NPOS + 1):
        forced = d.tok.decode([d.man["generated"][n - 1]["id"]])
        rows.append(f"| {n} | {forced!r} | " + " | ".join(top1_cell(d, m, n) for m in NAMES) + " |")
    return rows


def table_b(d: Data) -> list[str]:
    out = []
    for n in range(1, NPOS + 1):
        ex = d.olla[n - 1]["excluded"]
        out += [f"위치 {n} (강제 토큰 {d.tok.decode([d.man['generated'][n - 1]['id']])!r})"
                + (f" — Ollama {ex}" if ex else ""),
                "| 토큰 | float32 | 현미경 bf16 | HF sdpa | Ollama(2번째) | 현미경−f32 | sdpa−f32 | Ollama−f32 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for t in compare_tokens(d, n):
            v = {m: d.get(m, n, t, 1) for m in NAMES}
            df = [fmt(None if v[m] is None else v[m] - v["f32"]) for m in ("mic", "sdpa", "ollama")]
            out.append(f"| {t} {d.tok.decode([t])!r} | " + " | ".join(fmt(v[m]) for m in NAMES) + " | "
                       + " | ".join(df) + " |")
        out.append("")
    return out


def table_c(d: Data) -> list[str]:
    rows = ["| 비교 | 1등 같음 | 전체 평균\\|차\\| | 전체 최대\\|차\\| | 위치3 평균\\|차\\| | 위치3 최대\\|차\\| "
            "| 빠진 개수(전체/위치3) |", "| --- | --- | --- | --- | --- | --- | --- |"]
    pairs = [("mic", "f32"), ("sdpa", "f32"), ("ollama", "f32"), ("mic", "sdpa"), ("sdpa", "ollama"), ("mic", "ollama")]
    summary = {}
    for a, b in pairs:
        same = used = 0
        for n in range(1, NPOS + 1):
            ta, tb_ = d.top1(a, n), d.top1(b, n)
            if ta is not None and tb_ is not None:
                used += 1
                same += ta[0] == tb_[0]
        all_d, all_m = diffs(d, a, b, range(1, NPOS + 1))
        p3_d, p3_m = diffs(d, a, b, [3])
        sa, s3 = agg(all_d), agg(p3_d)
        summary[f"{a}_vs_{b}"] = {"top1_same": same, "top1_used": used, "all": sa, "pos3": s3,
                                  "missing_all": all_m, "missing_pos3": p3_m}
        f = lambda s, k: "-" if s is None else f"{s[k]:.4f}"  # noqa: E731
        rows.append(f"| {NAMES[a]} 대 {NAMES[b]} | {same}/{used} | {f(sa, 'mean_abs')} | {f(sa, 'max_abs')} "
                    f"| {f(s3, 'mean_abs')} | {f(s3, 'max_abs')} | {all_m}/{p3_m} |")
    d.summary = summary
    return rows


def table_d(d: Data) -> list[str]:
    rows = ["| 잡음 | 범위 | 개수 | 평균\\|차\\| | 최대\\|차\\| |", "| --- | --- | --- | --- | --- |"]
    res = {}
    for label, positions in (("전체", range(1, NPOS + 1)), ("위치3", [3])):
        o = [c2 - c1 for n in positions for k, c1 in d.olla_by_call[n - 1][0].items()
             if (c2 := d.olla_by_call[n - 1][1].get(k)) is not None]
        m = [d.mic[n - 1][t] - d.mic_f32head[n - 1][t] for n in positions for t in compare_tokens(d, n)
             if t in d.mic[n - 1] and t in d.mic_f32head[n - 1]]
        for name, v in (("Ollama 2번째−1번째 호출", o), ("현미경 bf16−f32 헤드(비교용 토큰)", m)):
            s = agg(v)
            res[f"{name}|{label}"] = s
            rows.append(f"| {name} | {label} | {0 if s is None else s['n']} | "
                        f"{'-' if s is None else format(s['mean_abs'], '.4f')} | "
                        f"{'-' if s is None else format(s['max_abs'], '.4f')} |")
    d.noise = res
    return rows


def main() -> None:
    guard.start_guard(40 * 60, label="precision_baseline")
    torch.set_num_threads(config.threads())
    torch.set_grad_enabled(False)
    man = json.loads((RUN_DIR / "manifest.json").read_text())
    ids, forced = man["prompt"]["token_ids"], [g["id"] for g in man["generated"]]
    if forced != EXPECTED or len(ids) != 72:
        raise SystemExit(f"manifest가 기대와 다르다: {forced} / 토큰 {len(ids)}")
    tok = chat.tokenizer()
    t_all = time.perf_counter()
    log(f"threads {torch.get_num_threads()} | 시작 메모리 {guard.proc_mem_gib()}")
    log("[Ollama Q4]")
    olla = run_ollama(tok, man, len(ids))
    log("[몸통 float32]")
    lp32, secs32 = run_f32_body(ids, forced)
    log("[HF sdpa bf16]")
    lpsdpa, secs_sdpa = run_hf_sdpa(ids, forced)
    d = Data(tok, man, lp32, lpsdpa, olla)
    sections = [("A) 위치별 1등 토큰과 확률", table_a(d)), ("B) 위치별 비교용 토큰 로그확률", table_b(d)),
                ("C) 방식별 요약(차이는 로그확률, float32 상위 5개 토큰 기준)", table_c(d)), ("D) 잡음", table_d(d))]
    for title, rows in sections:
        log(f"\n## {title}")
        log("\n".join(rows))
    detail = []
    for n in range(1, NPOS + 1):
        toks = compare_tokens(d, n)
        detail.append({"n": n, "forced_id": forced[n - 1], "compare_tokens": toks,
                       "values": {m: {str(t): d.get(m, n, t) for t in toks} for m in NAMES},
                       "values_ollama_call1": {str(t): d.get("ollama", n, t, 0) for t in toks},
                       "f32_top20": top20(tok, lp32[n - 1]), "sdpa_top20": top20(tok, lpsdpa[n - 1]),
                       "ollama": olla[n - 1]})
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"run": str(RUN_DIR), "forced": forced, "positions": detail, "summary": d.summary,
                                    "noise": d.noise, "seconds": {"f32_body": secs32, "hf_sdpa": secs_sdpa},
                                    "total_seconds": round(time.perf_counter() - t_all, 1),
                                    "rss_peak_gib": guard.proc_mem_gib()["VmHWM"]}, ensure_ascii=False, indent=1))
    log(f"\n저장: {OUT_PATH} | 전체 {time.perf_counter() - t_all:.1f}s | RSS 최대 {guard.proc_mem_gib()['VmHWM']}GiB")


if __name__ == "__main__":
    main()
