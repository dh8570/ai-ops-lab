#!/usr/bin/env python3
"""CPU 간섭 측정: LLM 추론이 도는 동안 앱·Watcher가 받는 영향을 잰다. 호스트에서 실행한다 (표준 라이브러리만).

    python3 scripts/spikes/llm-latency/interference.py

조건 A는 ollama CPU 제한 없음, 조건 B는 `docker update --cpus 4` 적용. 두 조건 모두 같은 호출을 한 번씩 한다
(qwen2.5:7b, 프롬프트 L, warm). 추론 호출은 compose 네트워크의 일회용 컨테이너에서 measure.py single로 보낸다.
조건마다
    1) measure.py prime으로 모델을 올려 warm 상태를 만든다
    2) 추론 전 60초: 2초 간격으로 앱 /healthz 응답 시간(호스트에서 Tailscale IP로 호출)을 기록한다
    3) 추론 중: 같은 간격으로 계속 기록한다. ollama 컨테이너 CPU(docker stats --no-stream)는 별도 스레드로 연속 기록한다
    4) 끝난 뒤 app.jsonl에서 path가 "/"인 요청의 p95(nearest-rank, Watcher와 같은 방식)를
       추론 전 60초와 추론 중으로 나눠 구한다
    5) 같은 시간대의 Watcher 이벤트(incident·notified·notify_dryrun·notify_failed, 불이 켜진 window)를 모은다
결과는 out/interference.json에 저장한다. 판정이나 결론은 내리지 않는다.
"""
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = HERE / "out"
NETWORK = "ai-ops-lab_default"
LOG_VOLUME = "ai-ops-lab_app-logs"
HEALTHZ_URL = "http://100.84.255.38:8000/healthz"        # 앱이 바인딩된 Tailscale IP (비밀값 아님)
MODEL, SIZE = "qwen2.5:7b", "L"
PRE_SEC, INTERVAL_SEC, HEALTHZ_TIMEOUT_SEC = 60, 2, 10
CONDITIONS = [("A", None), ("B", "4")]                    # (이름, --cpus 값 또는 제한 없음)
WATCHER_EVENTS = ("incident", "notified", "notify_dryrun", "notify_failed")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False, **kw)


def spike_cmd(*args: str) -> list[str]:
    return ["docker", "run", "--rm", "--network", NETWORK, "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1",
            "-v", f"{HERE}:/spike", "-v", f"{LOG_VOLUME}:/var/log/app:ro", "-w", "/spike",
            "python:3.12-slim", "python", "measure.py", *args]


def probe_healthz() -> dict:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(HEALTHZ_URL, timeout=HEALTHZ_TIMEOUT_SEC) as r:
            r.read()
            return {"t": time.time(), "s": time.perf_counter() - t0, "status": r.status}
    except Exception as e:                                # 시간 초과·연결 실패도 관찰값이다
        return {"t": time.time(), "s": None, "error": type(e).__name__, "waited": time.perf_counter() - t0}


class StatsThread(threading.Thread):
    """ollama 컨테이너 CPU%를 연속으로 기록한다 (docker stats --no-stream은 한 번에 1~2초 걸린다)."""

    def __init__(self, container: str):
        super().__init__(daemon=True)
        self.container, self.samples, self.stop = container, [], threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            p = run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", self.container])
            try:
                self.samples.append({"t": time.time(), "cpu_pct": float(p.stdout.strip().rstrip("%"))})
            except ValueError:
                pass


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "p50": sorted(values)[len(values) // 2] if values else None,
            "p95": p95(values), "max": max(values) if values else None}


def app_root_latencies(t0: float, t1: float) -> tuple[list[float], int]:
    """[t0, t1) 구간에서 path가 "/"인 요청의 latency_ms 목록과 5xx 건수."""
    p = run(["docker", "run", "--rm", "-v", f"{LOG_VOLUME}:/var/log/app:ro", "python:3.12-slim",
             "tail", "-n", "12000", "/var/log/app/app.jsonl"])
    vals, errors = [], 0
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["ts"]).timestamp()
        except (ValueError, KeyError):
            continue
        if t0 <= ts < t1 and e.get("path") == "/":
            vals.append(float(e["latency_ms"]))
            errors += int(e.get("status", 0) >= 500)
    return vals, errors


def watcher_events(since: float) -> list[dict]:
    iso = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = run(["docker", "compose", "logs", "--no-log-prefix", "--since", iso, "watcher"])
    found = []
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") in WATCHER_EVENTS or (e.get("event") == "window" and e.get("fired")):
            e.pop("payload", None)
            found.append(e)
    return found


def hz_stats(samples: list[dict]) -> dict:
    ok = [s["s"] * 1000 for s in samples if s.get("s") is not None]
    return {"n": len(samples), "failed": len(samples) - len(ok), **summarize(ok)}


def run_condition(name: str, cpus: str | None, container: str) -> dict:
    log(f"\n===== 조건 {name}: ollama CPU {'제한 없음' if cpus is None else f'--cpus {cpus}'} =====")
    upd = run(["docker", "update", "--cpus", cpus or "0", container])
    log(f"docker update --cpus {cpus or '0'} -> exit {upd.returncode}")
    prime = run(spike_cmd("prime", "--model", MODEL))
    log(prime.stdout.strip() or prime.stderr.strip())

    stats = StatsThread(container)
    stats.start()
    samples: list[dict] = []
    t_pre = time.time()
    log(f"[{datetime.now(UTC):%H:%M:%S}] 추론 전 {PRE_SEC}초 기록 시작")
    while time.time() - t_pre < PRE_SEC:
        samples.append(probe_healthz())
        time.sleep(INTERVAL_SEC)
    t_inf0 = time.time()
    log(f"[{datetime.now(UTC):%H:%M:%S}] 추론 시작 (measure.py single, {MODEL}, {SIZE}, warm)")
    proc = subprocess.Popen(spike_cmd("single", "--model", MODEL, "--size", SIZE, "--tag", f"interference-{name}"),
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    while proc.poll() is None:
        samples.append(probe_healthz())
        time.sleep(INTERVAL_SEC)
    t_inf1 = time.time()
    stats.stop.set()
    out = proc.stdout.read().strip()
    log(f"[{datetime.now(UTC):%H:%M:%S}] 추론 끝 ({t_inf1 - t_inf0:.0f}초), exit {proc.returncode}")
    try:
        inference = json.loads(out.splitlines()[-1])
        inference.pop("response", None)
    except (ValueError, IndexError):
        inference = {"raw_output": out[-500:]}

    time.sleep(5)                                          # 마지막 요청이 로그에 쓰일 시간
    pre_vals, pre_err = app_root_latencies(t_pre, t_inf0)
    inf_vals, inf_err = app_root_latencies(t_inf0, t_inf1)
    pre_hz = [s for s in samples if s["t"] < t_inf0]
    inf_hz = [s for s in samples if s["t"] >= t_inf0]
    cpu_pre = [s["cpu_pct"] for s in stats.samples if s["t"] < t_inf0]
    cpu_inf = [s["cpu_pct"] for s in stats.samples if s["t"] >= t_inf0]
    result = {
        "condition": name, "cpus": cpus, "model": MODEL, "size": SIZE,
        "window": {"pre_start": datetime.fromtimestamp(t_pre, UTC).isoformat(timespec="seconds"),
                   "inference_start": datetime.fromtimestamp(t_inf0, UTC).isoformat(timespec="seconds"),
                   "inference_end": datetime.fromtimestamp(t_inf1, UTC).isoformat(timespec="seconds"),
                   "inference_sec": round(t_inf1 - t_inf0, 1)},
        "inference": inference,
        "healthz_ms": {"pre": hz_stats(pre_hz), "during": hz_stats(inf_hz)},
        "healthz_samples": samples,
        "ollama_cpu_pct": {"pre": summarize(cpu_pre), "during": summarize(cpu_inf), "samples": stats.samples},
        "app_root_latency_ms": {"pre": {**summarize(pre_vals), "errors_5xx": pre_err},
                                "during": {**summarize(inf_vals), "errors_5xx": inf_err}},
        "watcher_events_since_pre_start": watcher_events(t_pre),
    }
    log(f"healthz(ms) 추론 전 {result['healthz_ms']['pre']} / 추론 중 {result['healthz_ms']['during']}")
    log(f"ollama CPU% 추론 전 {result['ollama_cpu_pct']['pre']} / 추론 중 {result['ollama_cpu_pct']['during']}")
    app_ms = result["app_root_latency_ms"]
    log(f"앱 / 요청 지연(ms) 추론 전 {app_ms['pre']} / 추론 중 {app_ms['during']}")
    log(f"Watcher 이벤트 {len(result['watcher_events_since_pre_start'])}건")
    for e in result["watcher_events_since_pre_start"]:
        log("  " + json.dumps(e, ensure_ascii=False))
    return result


def main() -> int:
    ps = run(["docker", "compose", "--profile", "llm", "ps", "-q", "ollama"])
    container = ps.stdout.strip()
    if not container:
        log("ollama 컨테이너가 실행 중이 아닙니다.")
        return 2
    OUT.mkdir(exist_ok=True)
    results = []
    try:
        for name, cpus in CONDITIONS:
            results.append(run_condition(name, cpus, container))
            (OUT / "interference.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
            if name != CONDITIONS[-1][0]:
                log(f"조건 사이 60초 쉬기 ({(datetime.now(UTC) + timedelta(seconds=60)):%H:%M:%S}까지)")
                time.sleep(60)
    finally:
        run(["docker", "update", "--cpus", "0", container])      # 제한을 원래대로 돌려 둔다
    return 0


if __name__ == "__main__":
    sys.exit(main())
