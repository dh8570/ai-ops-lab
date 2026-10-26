#!/usr/bin/env python3
"""측정 결과(out/)로 docs/spikes/w03-llm-latency.md를 만든다. 호스트에서 실행한다 (표준 라이브러리만).

    python3 scripts/spikes/llm-latency/report.py

표와 수치는 전부 원시 결과(out/results.jsonl, out/interference.json, out/prompts.json, out/meta.txt)에서 계산한다.
사람이 숫자를 옮겨 적다 틀리는 일을 없애려는 것이다. 이 문서에는 결론 칸이 없다. 해석과 결정은 PM이 한다.
"""
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = HERE / "out"
DOC = ROOT / "docs" / "spikes" / "w03-llm-latency.md"
SIZES = ("S", "M", "L")
MODELS = ("llama3.2:3b", "qwen2.5:7b", "llama3.1:8b")


def num(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def secs(v: float | None) -> str:
    return "-" if v is None else (f"{v:.2f}" if v < 10 else f"{v:.1f}")


def med(values: list[float]) -> float | None:
    return median(values) if values else None


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def matrix_rows(records: list[dict]) -> list[str]:
    head = ("| 모델 | 크기 | 조건 | 성공/계획 | 입력 토큰 | 입력 처리(s) | 생성 토큰 | 생성(s) | 생성 tok/s | 로드(s) "
            "| 총 시간(s) 중앙값 [범위] | 200토큰 상한 |")
    rows = [head, "|" + "---|" * 12]
    for model in MODELS:
        for size in SIZES:
            for cond in ("cold", "warm"):
                cell = [r for r in records if (r["model"], r["size"], r["cond"]) == (model, size, cond)]
                ok = [r for r in cell if r["status"] == "ok"]
                bad = sorted({r["status"] for r in cell if r["status"] not in ("ok", "skipped")})
                if not cell:
                    continue
                label = f"{len(ok)}/{len(cell)}"
                if bad:
                    label += " (" + ",".join("초과" if s == "timeout" else s for s in bad) + ")"
                if not ok:
                    rows.append(f"| {model} | {size} | {cond} | {label} |" + " - |" * 8)
                    continue
                totals = [r["total_s"] for r in ok]
                cap = sum(1 for r in ok if r.get("done_reason") == "length")
                rows.append(
                    f"| {model} | {size} | {cond} | {label} | {num(med([r['prompt_tokens'] for r in ok]), 0)} "
                    f"| {secs(med([r['prompt_s'] for r in ok]))} | {num(med([r['gen_tokens'] for r in ok]), 0)} "
                    f"| {secs(med([r['gen_s'] for r in ok]))} | {num(med([r['gen_tok_per_s'] for r in ok]))} "
                    f"| {secs(med([r['load_s'] for r in ok]))} "
                    f"| {secs(med(totals))} [{secs(min(totals))}–{secs(max(totals))}] | {cap}/{len(ok)} |")
    return rows


def prompt_rows(prompts: dict, records: list[dict]) -> list[str]:
    rows = ["| 크기 | 목표 토큰 | 로그 줄 수 | " + " | ".join(f"{m} 실제 입력 토큰(중앙값)" for m in MODELS) + " |",
            "|" + "---|" * (3 + len(MODELS))]
    for size in SIZES:
        p = prompts["sizes"][size]
        cells = []
        for model in MODELS:
            vals = [r["prompt_tokens"] for r in records if r["model"] == model and r["size"] == size
                    and r["status"] == "ok"]
            cells.append(num(med(vals), 0))
        rows.append(f"| {size} | {p['target_tokens']} | {p['lines']} | " + " | ".join(cells) + " |")
    return rows


def interference_rows(items: list[dict]) -> tuple[list[str], list[str]]:
    summary = ["| 조건 | ollama CPU 제한 | 추론 구간(s) | 총 시간(s) | 입력 토큰 | 입력 tok/s | 생성 토큰 "
               "| 생성 tok/s | 로드(s) |",
               "|" + "---|" * 9]
    detail = ["| 조건 | 구간 | healthz 표본(실패) | healthz p50(ms) | healthz p95(ms) | healthz 최대(ms) "
              "| ollama CPU% p50 / 최대 | 앱 `/` 요청 수 | `/` p50(ms) | `/` p95(ms) | `/` 최대(ms) | 5xx |",
              "|" + "---|" * 12]
    for it in items:
        inf = it["inference"]
        limit = "제한 없음" if it["cpus"] is None else f"--cpus {it['cpus']}"
        if "total_s" in inf:
            summary.append(f"| {it['condition']} | {limit} | {it['window']['inference_sec']} | {secs(inf['total_s'])} "
                           f"| {inf['prompt_tokens']} | {num(inf['prompt_tok_per_s'])} | {inf['gen_tokens']} "
                           f"| {num(inf['gen_tok_per_s'])} | {secs(inf['load_s'])} |")
        else:
            summary.append(f"| {it['condition']} | {limit} | {it['window']['inference_sec']} | 측정 실패 |"
                           + " - |" * 5)
        for phase, label in (("pre", "추론 전 60초"), ("during", "추론 중")):
            hz, cpu, app = it["healthz_ms"][phase], it["ollama_cpu_pct"][phase], it["app_root_latency_ms"][phase]
            detail.append(
                f"| {it['condition']} | {label} | {hz['n']} ({hz['failed']}) | {num(hz['p50'])} | {num(hz['p95'])} "
                f"| {num(hz['max'])} | {num(cpu['p50'], 0)} / {num(cpu['max'], 0)} | {app['n']} | {num(app['p50'])} "
                f"| {num(app['p95'])} | {num(app['max'])} | {app['errors_5xx']} |")
    return summary, detail


def compact_event(e: dict) -> str:
    keys = ("ts", "event", "action", "fired", "incident_id", "kind", "state", "trigger", "status", "error")
    return json.dumps({k: e[k] for k in keys if k in e}, ensure_ascii=False)


def watcher_events_between(since: str, until: str) -> list[dict]:
    p = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "--since", since, "--until", until, "watcher"],
                       cwd=ROOT, capture_output=True, text=True, check=False)
    found = []
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") in ("incident", "notified", "notify_dryrun", "notify_failed") or (
                e.get("event") == "window" and e.get("fired")):
            found.append(e)
    return found


def main() -> None:
    records = [r for r in load_jsonl(OUT / "results.jsonl") if r.get("mode") == "matrix"]
    prompts = json.loads((OUT / "prompts.json").read_text(encoding="utf-8"))
    interference = json.loads((OUT / "interference.json").read_text(encoding="utf-8"))
    meta = (OUT / "meta.txt").read_text(encoding="utf-8").rstrip()
    stamps = sorted(r["ts_utc"] for r in records)
    first, last = datetime.fromisoformat(stamps[0]), datetime.fromisoformat(stamps[-1])
    since = (first - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (last + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    matrix_events = watcher_events_between(since, until)
    summary, detail = interference_rows(interference)

    lines = [
        "# 3주차 스파이크: 로컬 LLM 응답 시간 측정",
        "",
        f"측정 기간(UTC): 매트릭스 {stamps[0]} ~ {stamps[-1]}. 이 문서의 표와 수치는 "
        "`scripts/spikes/llm-latency/report.py`가 원시 결과(`out/`, 레포에 올리지 않음)에서 계산해 만들었다.",
        "",
        "**해석과 결정은 PM이 한다.** 이 문서는 측정값만 적고 결론 칸을 두지 않는다.",
        "",
        "## 환경",
        "",
        "```",
        meta,
        "```",
        "",
        "## 측정 방식",
        "",
        "- Ollama `/api/generate` (stream false). 응답 필드 `load_duration`, `prompt_eval_count`, "
        "`prompt_eval_duration`, `eval_count`, `eval_duration`, `total_duration`을 나노초에서 초로 바꿔 쓴다.",
        "- options: `num_predict=200`, `temperature=0.2`, **`num_ctx=8192`** (지시서에 없던 값. L이 기본 "
        "컨텍스트 길이에 걸려 조용히 잘리는 것을 막으려고 모든 호출에 같게 고정했다).",
        "- cold: 메모리에서 모델을 내린 직후(`keep_alive=0` 후 `/api/ps`가 빌 때까지 확인)의 첫 호출. "
        "OS 페이지 캐시는 비우지 못해서, 모델 파일이 캐시에 있으면 로드 시간이 실제 첫 로드보다 짧을 수 있다.",
        "- warm: cold 호출 직후의 두 번째 호출.",
        "- 프롬프트 맨 앞에 호출마다 다른 8자리 표지를 붙였다 (KV 캐시 재사용으로 warm의 입력 처리가 "
        "짧아지는 것을 막으려고. 입력이 약 10토큰 늘어난다).",
        "- 반복: S·M은 3회, L은 2회 (회마다 cold→warm 한 쌍). "
        "한 호출이 10분을 넘으면 그 칸은 중단하고 \"초과\"로 적는다.",
        "- 실행은 compose 네트워크에 붙인 일회용 `python:3.12-slim` 컨테이너에서 했다 "
        "(ollama는 호스트에 게시하지 않았다).",
        "",
        "## 프롬프트 구성",
        "",
        f"지시문 + 임시 런북 + 실제 `app.jsonl` 최근 줄. 줄 수는 `{prompts['calibrated_on']}`로 맞췄고 "
        f"(고정부 {prompts['baseline_tokens']}토큰, 로그 한 줄당 약 {prompts['tokens_per_line']:.1f}토큰), "
        "모델마다 토크나이저가 달라 실제 입력 토큰 수가 다르다. "
        "**S는 고정부(지시문+런북)만으로 목표 300토큰을 넘는다.** "
        f"그래서 S는 로그 {prompts['sizes']['S']['lines']}줄만 붙였다.",
        "",
        *prompt_rows(prompts, records),
        "",
        "## 결과: 모델 × 크기 × cold/warm",
        "",
        "각 칸은 성공한 호출의 중앙값이다. 총 시간만 최소–최대 범위를 함께 적었다. "
        "\"200토큰 상한\"은 `done_reason == length`(생성이 200토큰에서 잘림)인 호출 수 / 성공한 호출 수.",
        "",
        *matrix_rows(records),
        "",
        "## 결과: CPU 간섭 (qwen2.5:7b, L, warm)",
        "",
        "조건 A는 ollama CPU 제한 없음, 조건 B는 `docker update --cpus 4`. 호출은 조건마다 한 번뿐이다.",
        "",
        *summary,
        "",
        *detail,
        "",
        "healthz는 호스트에서 Tailscale IP로 2초 간격 호출한 응답 시간(10초 안에 응답이 없으면 실패). "
        "앱 `/` 요청 지연은 `app.jsonl`의 `latency_ms`에서 path가 `/`인 요청만 모아 p95는 nearest-rank로 구했다.",
        "",
        "### 간섭 측정 중 Watcher 이벤트 (incident·notified·notify_dryrun·notify_failed, 불이 켜진 window)",
        "",
    ]
    for it in interference:
        lines += [f"조건 {it['condition']} (추론 전 60초 시작 {it['window']['pre_start']} 이후):", "", "```"]
        lines += [compact_event(e) for e in it["watcher_events_since_pre_start"]] or ["(없음)"]
        lines += ["```", ""]
    lines += [
        "### 매트릭스 측정 기간 중 Watcher 이벤트 (같은 필터)",
        "",
        f"`docker compose logs --since {since} --until {until} watcher`에서 같은 필터로 추린 {len(matrix_events)}건.",
        "",
        "```",
        *([compact_event(e) for e in matrix_events] or ["(없음)"]),
        "```",
        "",
        "## 사용한 명령",
        "",
        "```bash",
        "docker compose --profile llm up -d ollama                      # ollama/ollama:0.40.2, 포트 게시 없음",
        "docker compose --profile llm exec -T ollama ollama pull <모델>  # llama3.2:3b, qwen2.5:7b, llama3.1:8b",
        "# 측정 컨테이너 (compose 네트워크, 앱 로그 볼륨은 읽기 전용)",
        "docker run --rm --network ai-ops-lab_default --user \"$(id -u):$(id -g)\" \\",
        "  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \\",
        "  -v \"$PWD/scripts/spikes/llm-latency:/spike\" -v ai-ops-lab_app-logs:/var/log/app:ro \\",
        "  -w /spike python:3.12-slim python measure.py calibrate     # 이어서 matrix",
        "python3 scripts/spikes/llm-latency/interference.py             # CPU 간섭 (호스트에서)",
        "python3 scripts/spikes/llm-latency/report.py                   # 이 문서 생성",
        "```",
        "",
        "## 한계",
        "",
        "- 반복 횟수가 적다 (S·M 3회, L 2회, 간섭 측정은 조건마다 1회). 중앙값과 범위는 참고용이다.",
        "- 이 VM 한 대(위 환경)에서만 쟀다. 다른 CPU·메모리·동시 부하에서는 다를 수 있다.",
        "- 측정 중에도 `traffic`(5 rps)은 켜 둔 평소 상태였고, 매트릭스 구간에는 CPU 제한이 없었다.",
        "- cold는 OS 페이지 캐시를 비우지 못했다 (root 권한이 필요해 하지 않았다).",
        "- 프롬프트는 임시 런북과 정상 상태의 `app.jsonl` 줄로 만들었다. 장애 상황의 로그나 실제 런북이 아니다.",
        "- 응답 품질(원인 후보가 맞는지, 한국어가 자연스러운지)은 이 측정의 범위가 아니다.",
        "- `num_ctx=8192` 고정은 지시서에 없던 설정이다. 다른 값에서는 메모리와 속도가 달라질 수 있다.",
        "",
    ]
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(lines), encoding="utf-8")
    print(f"작성: {DOC.relative_to(ROOT)} ({len(lines)}줄)")


if __name__ == "__main__":
    main()
