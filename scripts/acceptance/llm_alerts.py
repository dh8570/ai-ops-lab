#!/usr/bin/env python3
"""LLM 알림 합격 시험 F·G·H와 R4 확인 (A1-T4). alerts.py의 A~E 시험에 이어지는 E2E 스크립트.

실행 위치: VM 호스트 (표준 라이브러리만 사용, Docker Compose 필요). 준비와 순서는 docs/acceptance/llm-scenarios.md.
    python3 scripts/acceptance/llm_alerts.py --scenario f     # Ollama 없음, 약 9분
    python3 scripts/acceptance/llm_alerts.py --scenario g     # LLM 느림(90초 지연), 약 9분
    python3 scripts/acceptance/llm_alerts.py --scenario h     # 정상, 장애 2번(같은 글 확인), 약 18분
    python3 scripts/acceptance/llm_alerts.py --scenario r4    # 앱 정지(R4)로 연 장애의 분석, 약 10분

증명할 것
    F  Ollama가 없어도 기본 알림이 간다.   FIRING 알림은 기존 A(시한)·B(정확히 1건)·C(복구)가 같은 조건으로 판정한다.
       여기에 llm_failed(connect_error) 1건, ANALYSIS_FAILED 알림 1건(FIRING 뒤), ANALYSIS 알림 0건을 더한다.
    G  LLM이 느려도 감시 루프가 멈추지 않는다.   가짜 서버가 90초 늦게 답한다. 창 처리 이벤트 사이 간격이
       window_sec+15초 이내이고, llm_failed(timeout)가 FIRING 25~40초 뒤에 1건, ANALYSIS_FAILED가 1건이다.
    H  정상일 때 분석이 장애당 한 번 후속으로 온다.   ANALYSIS가 장애당 정확히 1건이고 FIRING 알림 뒤에 온다.
       llm_runs.jsonl에는 그 장애의 기록이 한 줄(status ok, 옵션 7개)이고, 두 장애의 system_sha256이 같다.
    R4 앱 정지로 연 장애: 분석이 최대 1건이고, 기록의 사용자 글에 "[최근"이 없고 "로그 창이 닫히기 전에 열려"가 있다.
       A~E는 alerts.py의 stop 시나리오가 그대로 판정한다.

실제 Slack으로 보내지 않는 장치
    시작할 때 watcher의 start 이벤트를 읽어 notify_mode가 dryrun이 아니면 시험을 시작하지 않고 종료 코드 2로 끝난다.
    (watcher를 NOTIFY_WEBHOOK_URL을 비운 채 다시 만드는 명령은 문서에 있다.)

판정은 로그 기반이다(alerts.py와 같은 이유). 판정 함수(llm_view, rows_*, max_window_gap 등)는 로그 이벤트 목록을
입력으로 받는 순수 함수라서 합성 로그로 시험한다(watcher/tests/test_acceptance_llm.py).

종료 코드: 0 전부 PASS / 1 하나라도 FAIL / 2 사전 조건 불충족·실행 불가 / 130 사용자 중단
"""
import argparse
import json
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

import alerts
from alerts import log

FAULT_SEC = 120                              # 에러 주입·앱 정지 시간(초). alerts.py의 --quick·stop 기본값과 같다
SLOW_DELAY_SEC = 90                          # G: 가짜 서버 지연. watcher의 호출 상한(30초)보다 충분히 크다
TIMEOUT_MIN_SEC = 25                         # G: llm_failed(timeout)가 FIRING 뒤 이 범위에 와야 한다
TIMEOUT_MAX_SEC = 40
WINDOW_GAP_TOLERANCE_SEC = 15                # G: 창 처리 간격 허용 오차(로그 시각이 초 단위이고 틱이 15초라서)
H_ROUNDS = 2                                 # H: 장애 횟수. 둘째 장애에서도 system 글이 같은지 보려면 2번 필요
READY_WAIT_SEC = 180                         # H: 다음 장애 전에 NORMAL로 돌아오기를 기다리는 한도
FAKE_SERVICE = "fake-ollama"
FAKE_PROFILE = "llmtest"
TRACE_FILE = "/data/llm_runs.jsonl"          # watcher 컨테이너 안(watcher-state 볼륨)
OPTION_KEYS = ("temperature", "seed", "top_k", "top_p", "repeat_penalty", "num_predict", "num_ctx")
NOTIFY_KINDS = ("FIRING", "UPDATE", "RESOLVED", "ANALYSIS", "ANALYSIS_FAILED")
LLM_EVENTS = ("llm_start", "llm_done", "llm_failed", "llm_skipped", "notify_dropped",
              "warmup_done", "warmup_failed", "worker_error")

# fake-ollama 컨테이너 안에서 제어 주소를 부르는 코드. 인자: get | set <mode> <delay 또는 빈 문자열>
FAKE_CLIENT = """
import json, sys, urllib.request as u
a = sys.argv[1:]
url = 'http://127.0.0.1:11434/__mode'
if a[0] == 'get':
    r = u.urlopen(url, timeout=5)
else:
    d = {'mode': a[1]}
    if a[2]:
        d['delay'] = float(a[2])
    r = u.urlopen(u.Request(url, json.dumps(d).encode(), {'Content-Type': 'application/json'}), timeout=5)
print(r.read().decode())
"""


# ---------------------------------------------------------------- 순수 판정 함수 (로그 이벤트 목록 → 판정)

def llm_view(events: list[dict], incident_id) -> dict:
    """한 장애의 알림·LLM 이벤트를 종류별로 모은다. notify는 알림 종류별 이벤트, pos는 로그에서의 순서(색인)."""
    v = {"notify": {k: [] for k in NOTIFY_KINDS}, "pos": {k: [] for k in NOTIFY_KINDS},
         "llm_start": [], "llm_done": [], "llm_failed": [], "llm_skipped": [], "notify_dropped": [],
         "worker_error": [e for e in events if e["event"] == "worker_error"]}
    if incident_id is None:
        return v
    for i, e in enumerate(events):
        if e.get("incident_id") != incident_id:
            continue
        name = e["event"]
        if name in ("notified", "notify_dryrun") and e.get("kind") in v["notify"]:
            v["notify"][e["kind"]].append(e)
            v["pos"][e["kind"]].append(i)
        elif name in ("llm_start", "llm_done", "llm_failed", "llm_skipped", "notify_dropped"):
            v[name].append(e)
    return v


def _after_firing(v: dict, kind: str) -> bool:
    """그 종류 알림이 모두 첫 FIRING 알림보다 뒤(로그 순서)에 있는가. FIRING이 없으면 False."""
    if not v["pos"]["FIRING"]:
        return False
    first = v["pos"]["FIRING"][0]
    return all(p > first for p in v["pos"][kind])


def _counts(v: dict) -> str:
    n = v["notify"]
    return (f"ANALYSIS {len(n['ANALYSIS'])}건 · ANALYSIS_FAILED {len(n['ANALYSIS_FAILED'])}건 · "
            f"llm_done {len(v['llm_done'])}건 · llm_failed {len(v['llm_failed'])}건")


def max_window_gap(events: list[dict], start: datetime, end: datetime) -> float | None:
    """시험 구간의 창 처리(window) 이벤트 사이 최대 간격(초). 시작 직전 창과 구간 끝까지 포함한다.
    구간 안에 창이 없으면 None."""
    times = sorted(e["_dt"] for e in events if e["event"] == "window")
    before = [t for t in times if t < start]
    inside = [t for t in times if start <= t <= end]
    if not inside:
        return None
    points = ([before[-1]] if before else []) + inside + [end]
    return max((b - a).total_seconds() for a, b in zip(points, points[1:], strict=False))


def rows_f(v: dict) -> list[tuple]:
    reasons = [e.get("reason") for e in v["llm_failed"]]
    n = v["notify"]
    return [
        ("F1", reasons == ["connect_error"], f"llm_failed {len(reasons)}건 reason={reasons} (기대 connect_error 1건)"),
        ("F2", len(n["ANALYSIS_FAILED"]) == 1 and _after_firing(v, "ANALYSIS_FAILED"),
         f"ANALYSIS_FAILED 알림 {len(n['ANALYSIS_FAILED'])}건, FIRING 뒤에 옴 "
         f"{'예' if _after_firing(v, 'ANALYSIS_FAILED') else '아니오'} (기대 1건·예)"),
        ("F3", not n["ANALYSIS"] and not v["llm_done"], f"ANALYSIS 알림 {len(n['ANALYSIS'])}건 · "
                                                       f"llm_done {len(v['llm_done'])}건 (기대 0건)"),
        ("F4", not v["worker_error"], f"worker_error {len(v['worker_error'])}건 (기대 0건)"),
    ]


def rows_g(v: dict, max_gap: float | None, window_sec: int) -> list[tuple]:
    limit = window_sec + WINDOW_GAP_TOLERANCE_SEC
    gap_text = "측정 못 함" if max_gap is None else f"{max_gap:.0f}초"
    rows = [("G1", max_gap is not None and max_gap <= limit, f"창 처리 사이 최대 간격 {gap_text} (한도 {limit}초)")]
    fails, n = v["llm_failed"], v["notify"]
    timeouts = [e for e in fails if e.get("reason") == "timeout"]
    ok, text = False, f"llm_failed {len(fails)}건, 그중 timeout {len(timeouts)}건 (기대 timeout 1건)"
    if len(fails) == 1 and len(timeouts) == 1 and n["FIRING"]:
        gap = (fails[0]["_dt"] - n["FIRING"][0]["_dt"]).total_seconds()
        wall = fails[0].get("wall_ms")
        wall_ok = isinstance(wall, int | float) and TIMEOUT_MIN_SEC * 1000 <= wall <= TIMEOUT_MAX_SEC * 1000
        ok = TIMEOUT_MIN_SEC <= gap <= TIMEOUT_MAX_SEC and wall_ok
        text = (f"llm_failed(timeout) FIRING {gap:.0f}초 뒤, wall_ms {wall} "
                f"(기대 {TIMEOUT_MIN_SEC}~{TIMEOUT_MAX_SEC}초)")
    rows.append(("G2", ok, text))
    rows.append(("G3", len(n["ANALYSIS_FAILED"]) == 1 and not n["ANALYSIS"],
                 f"ANALYSIS_FAILED 알림 {len(n['ANALYSIS_FAILED'])}건 · ANALYSIS 알림 {len(n['ANALYSIS'])}건 "
                 "(기대 1건·0건)"))
    rows.append(("G4", not v["worker_error"], f"worker_error {len(v['worker_error'])}건 (기대 0건)"))
    return rows


def records_for(records: list[dict], incident_id) -> list[dict]:
    """llm_runs.jsonl 기록 중 그 장애의 것."""
    return [r for r in records
            if isinstance(r, dict) and isinstance(r.get("incident"), dict) and r["incident"].get("id") == incident_id]


def prompt_output(rec: dict) -> dict:
    """기록의 prompt 단계 output. 없으면 빈 dict."""
    for st in rec.get("stages") or []:
        if isinstance(st, dict) and st.get("id") == "prompt" and isinstance(st.get("output"), dict):
            return st["output"]
    return {}


def rows_h(v: dict, records: list[dict]) -> list[tuple]:
    n = v["notify"]
    one = records[0] if len(records) == 1 else {}
    settings = one.get("settings") if isinstance(one.get("settings"), dict) else {}
    missing = [k for k in OPTION_KEYS if settings.get(k) is None]
    return [
        ("H1", len(n["ANALYSIS"]) == 1 and _after_firing(v, "ANALYSIS"),
         f"ANALYSIS 알림 {len(n['ANALYSIS'])}건, FIRING 뒤에 옴 {'예' if _after_firing(v, 'ANALYSIS') else '아니오'} "
         "(기대 1건·예)"),
        ("H2", len(v["llm_done"]) == 1 and not v["llm_failed"] and not n["ANALYSIS_FAILED"], _counts(v)),
        ("H3", len(records) == 1 and one.get("status") == "ok" and not missing,
         f"llm_runs 기록 {len(records)}줄, status {one.get('status')!r}, 옵션 7개 중 없는 것 {missing} "
         "(기대 1줄·ok·없음)"),
    ]


def rows_h_hash(hashes: list) -> list[tuple]:
    """장애마다 모은 system_sha256이 모두 같은가(프리픽스 캐시가 쓰는 고정 글)."""
    same = len(hashes) >= 2 and None not in hashes and len(set(hashes)) == 1
    return [("H4", same, f"system_sha256 {len(hashes)}개 중 서로 다른 값 {len(set(hashes))}개 (기대 2개 이상 중 1개)")]


def rows_r4(v: dict, records: list[dict]) -> list[tuple]:
    n = v["notify"]
    total = len(n["ANALYSIS"]) + len(n["ANALYSIS_FAILED"])
    user = prompt_output(records[0]).get("user") if len(records) == 1 else None
    user_ok = isinstance(user, str) and "[최근" not in user and "로그 창이 닫히기 전에 열려" in user
    user_text = ("기록이 한 줄이 아니라 확인 못 함" if user is None
                 else f'"[최근" {"있음" if "[최근" in user else "없음"}, '
                      f'"로그 창이 닫히기 전에 열려" {"있음" if "로그 창이 닫히기 전에 열려" in user else "없음"}')
    return [
        ("S1", total <= 1 and len(v["llm_start"]) <= 1,
         f"분석 알림 {total}건 · llm_start {len(v['llm_start'])}건 (기대 각 1건 이하) · {_counts(v)}"),
        ("S2", len(records) == 1, f"llm_runs 기록 {len(records)}줄 (기대 1줄: 분석이 한 번 시도됨)"),
        ("S3", user_ok, f"사용자 글: {user_text} (기대 없음·있음)"),
    ]


def describe_llm(e: dict) -> str:
    keys = ("reason", "wall_ms", "kind", "flags", "late", "worker", "error", "trigger")
    extra = " ".join(f"{k}={e[k]}" for k in keys if k in e)
    return f"{e['event']:<14} inc={e.get('incident_id', '-')} {extra}".rstrip()


def print_rows(rows: list[tuple]) -> bool:
    ok_all = True
    for item, ok, text in rows:
        ok_all &= ok
        log(f"{item}  {'PASS' if ok else 'FAIL'}  {text}")
    return ok_all


# ---------------------------------------------------------------- 실행 보조 (docker compose)

def fake_exec(*args: str):
    return alerts.compose("--profile", FAKE_PROFILE, "exec", "-T", FAKE_SERVICE, "python", "-c", FAKE_CLIENT, *args)


def fake_mode() -> dict | None:
    p = fake_exec("get")
    try:
        return json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        return None


def set_fake_mode(mode: str, delay: float | None = None) -> bool:
    p = fake_exec("set", mode, "" if delay is None else str(delay))
    try:
        got = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        got = None
    ok = isinstance(got, dict) and got.get("mode") == mode
    log(f"[fake-ollama] 모드 {mode}{'' if delay is None else f' (지연 {delay:.0f}초)'} "
        f"{'설정함' if ok else '설정 실패: ' + p.stderr.strip()[:200]}")
    return ok


def fake_running() -> bool:
    p = alerts.compose("--profile", FAKE_PROFILE, "ps", "--services", "--status", "running")
    return p.returncode == 0 and FAKE_SERVICE in p.stdout.split()


def read_trace_records() -> list[dict] | None:
    """watcher 컨테이너의 llm_runs.jsonl을 읽는다. 읽지 못하면 None(파일이 아직 없을 때도 포함)."""
    p = alerts.compose("exec", "-T", "watcher", "cat", TRACE_FILE)
    if p.returncode != 0:
        return None
    out = []
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def llm_preconditions(scenario: str, window_sec: int) -> list[str]:
    """alerts.py의 사전 조건에 LLM 시험용 조건을 더한다. 불충족 항목을 문장으로 돌려준다."""
    problems = alerts.check_preconditions(window_sec)
    starts = [e for e in alerts.watcher_events() if e["event"] == "start"]
    if not starts:
        problems.append("watcher의 start 이벤트가 없음 (watcher가 방금 만들어졌다면 로그가 쌓일 때까지 기다리세요)")
    else:
        s = starts[-1]
        if s.get("notify_mode") != "dryrun":
            problems.append(f"watcher의 notify_mode가 {s.get('notify_mode')!r} (dryrun이어야 함). 실제 Slack으로 "
                            "나갈 수 있어 시험하지 않습니다. NOTIFY_WEBHOOK_URL을 비운 채 watcher를 다시 만드세요")
        if s.get("llm_enabled") is not True:
            problems.append("watcher의 llm_enabled가 켜져 있지 않음 (LLM_ENABLED=1로 watcher를 다시 만드세요)")
    if scenario != "f" and not fake_running():
        problems.append("fake-ollama 컨테이너가 실행 중이 아님 (docker compose --profile llmtest up -d fake-ollama)")
    return problems


def _since(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def inject_errors(fault_sec: int, window_sec: int, resolve_windows: int):
    """alerts.py의 errors 시나리오와 같은 방식(loadgen 5xx 100%)으로 장애를 한 번 주입하고 RESOLVED까지 지켜본다.
    (시작 시각, 주입 종료 시각, alerts.analyze 결과)를 돌려준다. loadgen을 못 띄우면 None."""
    alerts.cleanup()
    start = datetime.now(UTC)
    run = alerts.compose("run", "--rm", "-d", "--name", alerts.CONTAINER, "loadgen",
                         "--rps", "3", "--error-rate", "1.0", "--duration", str(fault_sec))
    if run.returncode != 0:
        log(f"loadgen 실행 실패: {run.stderr.strip()[:300]}")
        return None
    log(f"[T+   0s] {start.isoformat(timespec='seconds')} 에러 주입 시작")
    try:
        result, fault_end = alerts.observe(start, fault_sec, window_sec, resolve_windows)
    finally:
        alerts.cleanup()
    return start, fault_end, result


def collect(start: datetime, window_sec: int) -> tuple[list[dict], list[dict], datetime]:
    """(주입 이후 이벤트, 창 간격용 이벤트(시작 한 창 전부터), 지금 시각)."""
    now = datetime.now(UTC)
    wide = alerts.watcher_events("--since", _since(start - timedelta(seconds=window_sec + WINDOW_GAP_TOLERANCE_SEC)))
    return [e for e in wide if e["_dt"] >= start.replace(microsecond=0)], wide, now


def show_timeline(events: list[dict], start: datetime) -> None:
    log()
    log("== LLM 이벤트 ==")
    shown = [e for e in events if e["event"] in LLM_EVENTS]
    for e in shown:
        log(f"[T+{max(0, (e['_dt'] - start).total_seconds()):>4.0f}s] {describe_llm(e)}")
    if not shown:
        log("(없음)")


def one_round(label: str, fault_sec: int, window_sec: int, resolve_windows: int):
    """에러 주입 한 번. (A~C 판정 종료 코드, 장애 id, 이벤트 view, 창 간격, 시작 시각) 또는 None."""
    out = inject_errors(fault_sec, window_sec, resolve_windows)
    if out is None:
        return None
    start, fault_end, result = out
    events, wide, now = collect(start, window_sec)
    rc = alerts.judge(result, start, fault_end, fault_sec, window_sec, resolve_windows, False)
    log()
    log(f"== LLM 결과 ({label}) ==")
    inc = alerts.analyze(events)["incident_id"]
    show_timeline(events, start)
    return rc, inc, llm_view(events, inc), max_window_gap(wide, start, now), start


def finish(name: str, ok: bool) -> int:
    """alerts.judge는 A~C(R4는 A~E)만 보고 '전체'를 찍으므로, LLM 항목까지 합친 최종 줄을 따로 남긴다."""
    log(f"전체({name}, 위 기존 판정 포함): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def run_f(fault_sec, window_sec, resolve_windows) -> int:
    got = one_round("F 가짜 서버 없음", fault_sec, window_sec, resolve_windows)
    if got is None:
        return 2
    rc, _, v, _, _ = got
    return finish("F", print_rows(rows_f(v)) and rc == 0)


def run_g(fault_sec, window_sec, resolve_windows) -> int:
    got = one_round("G 지연 90초", fault_sec, window_sec, resolve_windows)
    if got is None:
        return 2
    rc, _, v, gap, _ = got
    return finish("G", print_rows(rows_g(v, gap, window_sec)) and rc == 0)


def wait_ready(window_sec: int) -> list[str]:
    """다음 장애를 넣어도 되는 상태(NORMAL 등)가 될 때까지 기다린다. 끝내 안 되면 남은 문제를 돌려준다."""
    deadline = time.monotonic() + READY_WAIT_SEC
    while True:
        problems = alerts.check_preconditions(window_sec)
        if not problems or time.monotonic() > deadline:
            return problems
        time.sleep(10)


def run_h(fault_sec, window_sec, resolve_windows) -> int:
    ok_all, hashes = True, []
    for n in range(1, H_ROUNDS + 1):
        if n > 1:
            log(f"\n다음 장애 전에 NORMAL 상태로 돌아오기를 기다립니다 (최대 {READY_WAIT_SEC}초)")
            problems = wait_ready(window_sec)
            if problems:
                log("사전 조건 불충족 - 두 번째 장애를 시작하지 않습니다:")
                for p in problems:
                    log(f"  - {p}")
                return 2
        got = one_round(f"H {n}번째 장애", fault_sec, window_sec, resolve_windows)
        if got is None:
            return 2
        rc, inc, v, _, _ = got
        records = read_trace_records()
        if records is None:
            log(f"[주의] {TRACE_FILE}을 읽지 못했습니다(watcher 컨테이너, 파일 없음 포함)")
        mine = records_for(records or [], inc)
        hashes.append(prompt_output(mine[0]).get("system_sha256") if len(mine) == 1 else None)
        ok_all &= print_rows(rows_h(v, mine)) and rc == 0
    log()
    log("== LLM 결과 (H 장애 간 비교) ==")
    ok_all &= print_rows(rows_h_hash(hashes))
    return finish("H", ok_all)


def run_r4(fault_sec, window_sec, resolve_windows) -> int:
    before = datetime.now(UTC)
    rc = alerts.run_stop_scenario(fault_sec, window_sec, resolve_windows)    # A~E를 alerts.py가 그대로 판정한다
    if rc in (2, 130):
        return rc
    events = alerts.watcher_events("--since", _since(before))
    inc = alerts.analyze(events)["incident_id"]
    log()
    log("== LLM 결과 (R4 앱 정지) ==")
    show_timeline(events, before)
    records = read_trace_records()
    if records is None:
        log(f"[주의] {TRACE_FILE}을 읽지 못했습니다(watcher 컨테이너, 파일 없음 포함)")
    ok = print_rows(rows_r4(llm_view(events, inc), records_for(records or [], inc)))
    return finish("R4", ok and rc == 0)


SCENARIOS = {  # 이름: (실행 함수, (시작할 때 가짜 서버 모드, 지연 초))
    "f": (run_f, ("analysis", None)),
    "g": (run_g, ("slow", SLOW_DELAY_SEC)),
    "h": (run_h, ("analysis", None)),
    "r4": (run_r4, ("analysis", None)),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 알림 합격 시험: F(Ollama 없음)·G(LLM 느림)·H(정상)·R4(앱 정지)")
    ap.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    ap.add_argument("--fault-sec", type=int, default=FAULT_SEC, help=f"주입(정지) 시간(초), 기본 {FAULT_SEC}")
    args = ap.parse_args()
    run, (mode, delay) = SCENARIOS[args.scenario]

    window_sec, resolve_windows = alerts.load_params()
    problems = llm_preconditions(args.scenario, window_sec)
    if problems:
        log("사전 조건 불충족 - 시험을 시작하지 않습니다:")
        for p in problems:
            log(f"  - {p}")
        return 2

    signal.signal(signal.SIGTERM, alerts._raise_interrupt)
    use_fake = fake_running()
    if use_fake and not set_fake_mode(mode, delay):
        if args.scenario != "f":                     # F는 가짜 서버를 쓰지 않으므로 못 바꿔도 계속한다
            return 2
    log(f"시험 시작: LLM 시나리오 {args.scenario.upper()} · 주입 {args.fault_sec}초 · window {window_sec}초 · "
        f"resolve_windows {resolve_windows} · 알림은 dryrun")
    try:
        return run(args.fault_sec, window_sec, resolve_windows)
    except KeyboardInterrupt:
        log("\n중단됨")
        return 130
    finally:
        if use_fake:                                 # 어떤 경우에도 가짜 서버를 정상 모드로 되돌린다
            set_fake_mode("analysis")


if __name__ == "__main__":
    sys.exit(main())
