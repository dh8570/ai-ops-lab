#!/usr/bin/env python3
"""로컬 LLM(Ollama) 응답 시간 측정 스파이크 (3주차 선행 과제). 표준 라이브러리만 사용한다.

측정만 하고 결론은 내리지 않는다. 결과는 3주차 설계(시간 제한, 알림 방식, 로그 줄 수)의 입력이다.

실행: Ollama를 호스트에 게시하지 않으므로 compose 네트워크에 붙인 일회용 컨테이너에서 돌린다.
    docker run --rm --network ai-ops-lab_default --user "$(id -u):$(id -g)" \
        -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
        -v "$PWD/scripts/spikes/llm-latency:/spike" -v ai-ops-lab_app-logs:/var/log/app:ro \
        -w /spike python:3.12-slim python measure.py <calibrate|matrix|prime|single> [옵션]

하위 명령
    calibrate  프롬프트 S·M·L의 로그 줄 수를 맞춰 out/prompts.json에 저장한다 (모델 하나로 토큰 수를 잰다)
    matrix     모델 × 크기 × cold/warm 전체 측정. 결과는 out/results.jsonl에 한 줄씩 쌓는다
    prime      모델을 메모리에 올린다 (측정 아님). CPU 간섭 측정 전에 warm 상태를 만들 때 쓴다
    single     한 번만 측정한다 (CPU 간섭 측정용, 모델이 이미 올라가 있다고 가정)

측정 방식
    Ollama /api/generate (stream false)의 응답 필드를 쓴다. 시간은 나노초 → 초로 바꾼다.
    options: num_predict=200, temperature=0.2, num_ctx=8192
    num_ctx는 지시서에 없던 값이다. L(약 4,000토큰)이 기본 컨텍스트 길이에 걸려 조용히 잘리는 것을 막으려고
    모든 호출에 같은 값으로 고정했다 (값이 호출마다 다르면 모델이 다시 올라가 cold로 오염된다).
    cold  메모리에서 모델을 내린 직후(keep_alive=0 후 /api/ps가 빌 때까지 확인)의 첫 호출.
          OS 페이지 캐시는 비우지 못한다 (모델 파일이 이미 캐시에 있으면 로드가 빠를 수 있다)
    warm  cold 호출 직후의 두 번째 호출
    프롬프트 맨 앞에 호출마다 다른 8자리 표지를 붙인다. Ollama가 같은 앞부분의 KV 캐시를 재사용하면
    warm의 입력 처리 시간이 실제보다 짧게 나오기 때문이다 (표지 때문에 입력이 약 10토큰 늘어난다).
    한 호출이 10분을 넘으면 그 칸은 "timeout"으로 기록하고 남은 반복은 "skipped"로 둔다.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

BASE = os.environ.get("OLLAMA_URL", "http://ollama:11434")
APP_LOG = Path(os.environ.get("APP_LOG", "/var/log/app/app.jsonl"))
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PROMPTS = OUT / "prompts.json"

MODELS = ["llama3.2:3b", "qwen2.5:7b", "llama3.1:8b"]
CALIBRATION_MODEL = "llama3.2:3b"
TARGET_TOKENS = {"S": 300, "M": 1500, "L": 4000}
MIN_LINES = 1                                # S는 고정부(지시문+런북, 약 300토큰)만으로 목표에 닿아 로그는 1줄만 붙인다
REPS = {"S": 3, "M": 3, "L": 2}
OPTIONS = {"num_predict": 200, "temperature": 0.2, "num_ctx": 8192}
TIMEOUT_SEC = 600

INSTRUCTION = "아래 로그와 런북을 보고 원인 후보 2~3개와 먼저 확인할 것을 한국어로 간결하게 써라."
RUNBOOK = """\
1) 5xx 급증: error 필드와 path를 먼저 본다.
   DB 연결 오류(OperationalError)면 db 컨테이너 상태와 연결 수를 확인한다.
2) 지연 증가(p95 2초 초과): gunicorn 워커가 2개뿐이라
   느린 요청(/slow) 몇 건이 워커를 점유하면 대기열이 쌓인다.
   latency_ms가 큰 요청의 path와 동시 요청 수를 본다.
3) 처리량 급감(기준선의 70% 미만): 앱 정지, 포화,
   클라이언트 쪽 문제 순으로 의심한다. 요청이 0건이면 앱 프로세스부터 확인한다.
4) /healthz 연속 실패: 컨테이너가 재시작 중인지, 포트 바인딩이 살아 있는지 확인한다.
5) 포화 중에는 다른 에러가 대기열에 묶였다가 해소될 때 희석될 수 있다.
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(msg, flush=True)


def call(path: str, body: dict | None = None, timeout: float = TIMEOUT_SEC) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def generate(model: str, prompt: str, num_predict: int | None = None) -> tuple[dict, float]:
    options = dict(OPTIONS)
    if num_predict is not None:
        options["num_predict"] = num_predict
    t0 = time.perf_counter()
    res = call("/api/generate", {"model": model, "prompt": prompt, "stream": False, "options": options})
    return res, time.perf_counter() - t0


def loaded_models() -> list[str]:
    return [m["name"] for m in call("/api/ps", timeout=30).get("models", [])]


def unload_all() -> bool:
    """메모리에 올라간 모델을 모두 내리고, /api/ps가 빌 때까지 확인한다 (cold 보장)."""
    for name in loaded_models():
        call("/api/generate", {"model": name, "keep_alive": 0}, timeout=60)
    for _ in range(30):
        if not loaded_models():
            return True
        time.sleep(1)
    return False


def tail_lines(path: Path, n: int, max_bytes: int = 2_000_000) -> list[str]:
    """app.jsonl의 마지막 n개 완전한 JSON 줄. 파일 끝의 쓰는 중인 줄과 잘린 첫 줄은 버린다."""
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - max_bytes))
        data = f.read()
    good = []
    for raw in data.splitlines()[1:-1]:
        try:
            json.loads(raw)
        except ValueError:
            continue
        good.append(raw.decode())
    return good[-n:]


def prompt_body(lines: list[str]) -> str:
    return (f"{INSTRUCTION}\n\n[런북]\n{RUNBOOK}\n[최근 로그 {len(lines)}줄]\n" + "\n".join(lines))


def with_marker(body: str) -> str:
    return f"[측정 {uuid.uuid4().hex[:8]}]\n{body}"


def load_prompts() -> dict:
    if not PROMPTS.exists():
        sys.exit("out/prompts.json이 없습니다. 먼저 calibrate를 실행하세요.")
    return json.loads(PROMPTS.read_text())


def make_record(meta: dict, res: dict | None, wall: float, status: str) -> dict:
    rec = {"ts_utc": now_iso(), **meta, "status": status, "wall_s": round(wall, 3)}
    if res is None:
        return rec

    def sec(key: str) -> float:
        return res.get(key, 0) / 1e9

    gen_s, prompt_s = sec("eval_duration"), sec("prompt_eval_duration")
    rec.update({
        "load_s": sec("load_duration"), "prompt_tokens": res.get("prompt_eval_count", 0), "prompt_s": prompt_s,
        "gen_tokens": res.get("eval_count", 0), "gen_s": gen_s, "total_s": sec("total_duration"),
        "gen_tok_per_s": (res.get("eval_count", 0) / gen_s) if gen_s else None,
        "prompt_tok_per_s": (res.get("prompt_eval_count", 0) / prompt_s) if prompt_s else None,
        "done_reason": res.get("done_reason"), "response": res.get("response", ""),
    })
    return rec


def timed_call(model: str, body: str, meta: dict) -> dict:
    """한 번 호출해 기록으로 만든다. 10분을 넘으면 timeout, 그 밖의 실패는 error로 기록한다."""
    t0 = time.perf_counter()
    try:
        res, wall = generate(model, with_marker(body))
        return make_record(meta, res, wall, "ok")
    except TimeoutError:
        return make_record(meta, None, time.perf_counter() - t0, "timeout")
    except urllib.error.URLError as e:
        status = "timeout" if isinstance(e.reason, TimeoutError) else "error"
        rec = make_record(meta, None, time.perf_counter() - t0, status)
        rec["error"] = str(e.reason)[:200]
        return rec


def describe(rec: dict) -> str:
    head = f"{rec['model']} {rec.get('size', '-')} {rec.get('cond', '-')} rep{rec.get('rep', '-')}"
    if rec["status"] != "ok":
        return f"{head} -> {rec['status'].upper()} (경과 {rec['wall_s']:.0f}초)"
    rate = rec["gen_tok_per_s"]
    return (f"{head} -> 총 {rec['total_s']:.1f}s | 로드 {rec['load_s']:.1f}s | "
            f"입력 {rec['prompt_tokens']}tok/{rec['prompt_s']:.1f}s | "
            f"생성 {rec['gen_tokens']}tok/{rec['gen_s']:.1f}s ({rate:.1f} tok/s) | done={rec['done_reason']}")


def append(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cmd_calibrate(args: argparse.Namespace) -> None:
    lines = tail_lines(APP_LOG, 400)
    log(f"[calibrate] 모델 {args.model}, app.jsonl 최근 {len(lines)}줄 사용")

    def count(n: int) -> int:
        res, wall = generate(args.model, with_marker(prompt_body(lines[-n:] if n else [])), num_predict=1)
        log(f"[calibrate] 로그 {n}줄 -> 입력 {res['prompt_eval_count']}토큰 ({wall:.1f}s)")
        return res["prompt_eval_count"]

    base, forty = count(0), count(40)
    per_line = (forty - base) / 40
    log(f"[calibrate] 고정부(지시문+런북+머리말) {base}토큰, 로그 한 줄당 약 {per_line:.1f}토큰")
    sizes = {}
    for size, target in TARGET_TOKENS.items():
        n = max(MIN_LINES, round((target - base) / per_line))
        actual = count(n)
        if abs(actual - target) > 0.05 * target and target > base:
            n = max(MIN_LINES, n + round((target - actual) / per_line))
            actual = count(n)
        sizes[size] = {"target_tokens": target, "lines": n, "calibration_tokens": actual,
                       "body": prompt_body(lines[-n:])}
    OUT.mkdir(exist_ok=True)
    PROMPTS.write_text(json.dumps({"calibrated_on": args.model, "baseline_tokens": base,
                                   "tokens_per_line": per_line, "sizes": sizes}, ensure_ascii=False, indent=1))
    for size, s in sizes.items():
        log(f"[calibrate] {size}: 목표 {s['target_tokens']} / 로그 {s['lines']}줄 / 실제 {s['calibration_tokens']}토큰")


def cmd_matrix(args: argparse.Namespace) -> None:
    prompts = load_prompts()["sizes"]
    models = args.models or MODELS
    sizes = args.sizes or list(TARGET_TOKENS)
    results = Path(args.results)
    total = sum(args.reps or REPS[s] for s in sizes) * 2 * len(models)
    done = 0
    log(f"[matrix] 모델 {models} / 크기 {sizes} / 호출 {total}회 / 결과 {results}")
    for model in models:
        for size in sizes:
            aborted = False
            for rep in range(1, (args.reps or REPS[size]) + 1):
                for cond in ("cold", "warm"):
                    done += 1
                    meta = {"mode": args.mode, "model": model, "size": size, "cond": cond, "rep": rep,
                            "lines": prompts[size]["lines"]}
                    if aborted:
                        rec = make_record(meta, None, 0, "skipped")
                    else:
                        if cond == "cold" and not unload_all():
                            log("[matrix] 경고: 모델을 내리지 못했습니다 (cold가 아닐 수 있음)")
                        rec = timed_call(model, prompts[size]["body"], meta)
                        if rec["status"] != "ok":
                            aborted = True
                            unload_all()
                    append(results, rec)
                    log(f"[{done:>2}/{total}] {describe(rec)}")
    unload_all()


def cmd_prime(args: argparse.Namespace) -> None:
    res, wall = generate(args.model, with_marker("안녕"), num_predict=1)
    load_s = res.get("load_duration", 0) / 1e9
    log(f"[prime] {args.model} 로드 {load_s:.1f}s (총 {wall:.1f}s), 올라간 모델 {loaded_models()}")


def cmd_single(args: argparse.Namespace) -> None:
    prompt = load_prompts()["sizes"][args.size]
    meta = {"mode": args.tag, "model": args.model, "size": args.size, "cond": "warm", "rep": 1,
            "lines": prompt["lines"]}
    rec = timed_call(args.model, prompt["body"], meta)
    append(Path(args.results), rec)
    log(json.dumps(rec, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("calibrate")
    p.add_argument("--model", default=CALIBRATION_MODEL)
    p = sub.add_parser("matrix")
    p.add_argument("--models", nargs="+")
    p.add_argument("--sizes", nargs="+", choices=list(TARGET_TOKENS))
    p.add_argument("--reps", type=int, help="반복 횟수를 모든 크기에 같게 덮어쓴다(스모크 시험용)")
    p.add_argument("--results", default=str(OUT / "results.jsonl"))
    p.add_argument("--mode", default="matrix")
    p = sub.add_parser("prime")
    p.add_argument("--model", required=True)
    p = sub.add_parser("single")
    p.add_argument("--model", required=True)
    p.add_argument("--size", choices=list(TARGET_TOKENS), default="L")
    p.add_argument("--tag", default="single")
    p.add_argument("--results", default=str(OUT / "results.jsonl"))
    args = ap.parse_args()
    {"calibrate": cmd_calibrate, "matrix": cmd_matrix, "prime": cmd_prime, "single": cmd_single}[args.cmd](args)


if __name__ == "__main__":
    main()
