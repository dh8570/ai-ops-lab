#!/usr/bin/env python3
"""v0 합격 시험 중 알림 항목(A·B·C)을 자동으로 검증하는 E2E 스크립트.

실행 위치: VM 호스트 (표준 라이브러리만 사용, Docker Compose 필요)
    python3 scripts/acceptance/alerts.py                   # errors: 10분 주입, 약 15분 소요
    python3 scripts/acceptance/alerts.py --quick           # errors: 2분 주입, 빠른 확인용 (B는 참고만)
    python3 scripts/acceptance/alerts.py --scenario stop   # stop: 앱 2분 정지, 약 9분 소요 (--quick 불가)

검증 항목
    A  탐지 시간   주입 시작 → 첫 FIRING 알림까지 120초 이내 (stop 시나리오는 60초)
    B  중복 없음   그 장애의 FIRING 알림이 정확히 1건
    C  복구 알림   주입 종료 후 RESOLVED 알림 도착
                   (대기 한도 = 주입 종료 + (resolve_windows + 2) × window_sec)
    D  UPDATE 없음 (stop 시나리오만) 그 장애의 UPDATE 알림이 0건. 프로브로 연 장애에 R3가 유예(180초) 안에
                   뒤따라 붙어도 UPDATE가 나가면 안 된다. 2-C에서 고친 버그(75초 뒤 R3가 붙자 UPDATE 발송)의 회귀 시험

시나리오 (--scenario)
    사전 조건(traffic 실행 중, 최근 window가 NORMAL, 기준선 3개 창 이상)을 확인한 뒤 장애를 주입하고
    Watcher 로그를 따라간다. 사전 조건이 하나라도 안 맞으면 시험을 시작하지 않고 이유를 출력한 뒤
    종료 코드 2로 끝난다.
    errors  loadgen으로 5xx 100% 에러를 fault_sec 동안 주입한다. 앱이 살아 있어 로그 창(R1)으로 장애가 열리고
            R4(합성 프로브) 경로는 거치지 않는다. (기본)
    stop    docker compose stop app으로 앱을 fault_sec(기본 120초) 동안 멈췄다가 start app으로 되살린다.
            로그가 끊기고 프로브가 실패하므로 R4 경로로 장애가 열려야 한다. A의 한도는 프로브 15초 × 2회
            연속 실패에 여유를 더한 60초이고, "R4 즉시 장애: 예/아니오"는 참고로만 출력한다.
            Ctrl+C나 예외로 끝나도 finally에서 start app을 실행해 앱이 멈춘 채 남지 않게 한다.

왜 로그 기반으로 판정하는가
    알림의 진짜 도착지는 Slack이지만 Slack 화면은 자동으로 읽을 수 없고, 채널이 바뀌어도(Discord 등) 같은
    시험이 돌아가야 한다. Watcher는 알림을 보낼 때마다 `notified`(전송 성공) 또는 `notify_dryrun`(웹훅 없음),
    실패하면 `notify_failed`를 JSON 한 줄로 남기고 장애 id를 함께 적는다. 그래서 "장애 id별 알림 이벤트"를
    세면 채널과 무관하게 A·B·C를 같은 방식으로 판정할 수 있다. 부품을 바꾸는 단계마다 같은 기준으로
    돌리는 회귀 게이트이므로, 사람이 눈으로 보는 확인에 의존하지 않는다.

측정 오차
    Watcher 로그의 ts는 초 단위라 탐지·복구 시간은 ±1초, 주입 종료 시각은 1초 간격 폴링이라 약 +1초 늦게 잡힌다.

종료 코드: 0 전부 PASS / 1 하나라도 FAIL / 2 사전 조건 불충족·실행 불가 / 130 사용자 중단
"""
import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = "acceptance-alerts-loadgen"      # 정리 대상을 이름으로 특정하기 위해 고정
DETECT_LIMIT_SEC = 120                       # A의 한도
STOP_DETECT_LIMIT_SEC = 60                   # stop 시나리오 A의 한도 (R4 프로브 15초 × 2회 + 여유)
POLL_SEC = 5                                 # 로그를 읽는 주기
DEFAULT_WINDOW_SEC = 60                      # start 이벤트를 못 찾았을 때 쓰는 Watcher 기본값
DEFAULT_RESOLVE_WINDOWS = 5
MIN_BASELINE_N = 3
SHOWN_EVENTS = ("window", "incident", "probe", "notified", "notify_dryrun", "notify_failed")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def watcher_events(*log_args: str) -> list[dict]:
    """Watcher 로그에서 JSON 줄만 골라 이벤트 목록으로 돌려준다. 시각은 `_dt`에 datetime으로 붙인다."""
    p = compose("logs", "--no-log-prefix", *log_args, "watcher")
    if p.returncode != 0:
        raise RuntimeError(f"docker compose logs 실패: {p.stderr.strip()[:200]}")
    events = []
    for line in p.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
            dt = datetime.fromisoformat(e["ts"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if not isinstance(e, dict) or "event" not in e:
            continue
        e["_dt"] = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        events.append(e)
    return events


def load_params() -> tuple[int, int]:
    """Watcher가 실제로 쓰는 window_sec, resolve_windows를 마지막 start 이벤트에서 읽는다."""
    starts = [e for e in watcher_events() if e["event"] == "start"]
    if not starts:
        log(f"[주의] start 이벤트를 찾지 못해 기본값(window {DEFAULT_WINDOW_SEC}s, "
            f"resolve {DEFAULT_RESOLVE_WINDOWS})을 씁니다.")
        return DEFAULT_WINDOW_SEC, DEFAULT_RESOLVE_WINDOWS
    s = starts[-1]
    return int(s.get("window_sec", DEFAULT_WINDOW_SEC)), int(s.get("resolve_windows", DEFAULT_RESOLVE_WINDOWS))


def check_preconditions(window_sec: int) -> list[str]:
    """불충족 항목을 문장으로 모아 돌려준다. 비어 있으면 시험을 시작해도 된다."""
    problems = []

    ps = compose("ps", "--services", "--status", "running")
    if ps.returncode != 0:
        problems.append(f"docker compose ps 실패: {ps.stderr.strip()[:200]}")
    elif "traffic" not in ps.stdout.split():
        problems.append("traffic 서비스가 실행 중이 아님 (docker compose --profile traffic up -d traffic)")

    windows = [e for e in watcher_events("--tail", "300") if e["event"] == "window"]
    if not windows:
        problems.append("최근 watcher window 이벤트가 없음 (watcher가 실행 중인지 확인)")
        return problems
    last = windows[-1]
    age = (datetime.now(UTC) - last["_dt"]).total_seconds()
    if age > 3 * window_sec:
        problems.append(f"마지막 window 이벤트가 {age:.0f}초 전이라 watcher가 멈춘 것으로 보임")
    if last.get("state") != "NORMAL":
        problems.append(
            f"최근 window의 state가 {last.get('state')} (incident {last.get('incident_id')}), NORMAL이 아님")
    if last.get("baseline_n", 0) < MIN_BASELINE_N:
        problems.append(f"최근 window의 baseline_n={last.get('baseline_n')}, {MIN_BASELINE_N} 미만")
    return problems


def analyze(events: list[dict]) -> dict:
    """주입 이후 이벤트에서 장애 id를 확정하고 그 장애의 알림 이벤트를 종류별로 모은다."""
    incident_id = next(
        (e["incident_id"] for e in events
         if e["event"] in ("window", "incident") and e.get("action") == "fire" and e.get("incident_id") is not None),
        None)
    r = {"incident_id": incident_id, "FIRING": [], "UPDATE": [], "RESOLVED": [], "failed": [], "r4_immediate": []}
    if incident_id is None:
        return r
    for e in events:
        if e.get("incident_id") != incident_id:
            continue
        if e["event"] in ("notified", "notify_dryrun") and e.get("kind") in r:
            r[e["kind"]].append(e)
        elif e["event"] == "notify_failed":
            r["failed"].append(e)
        elif e["event"] == "incident" and e.get("trigger") == "R4":
            r["r4_immediate"].append(e)             # 창을 기다리지 않고 R4 프로브가 바로 연 장애
    return r


def describe(e: dict) -> str | None:
    kind = e["event"]
    if kind == "window":
        return (f"window   req={e.get('requests')} err={e.get('error_rate')} p95={e.get('p95_ms')}ms "
                f"rpm={e.get('rpm')} fired={e.get('fired')} action={e.get('action')} "
                f"state={e.get('state')} inc={e.get('incident_id')}")
    if kind in ("notified", "notify_dryrun"):
        return f"{kind:<8} kind={e.get('kind')} inc={e.get('incident_id')} status={e.get('status', '-')}"
    if kind == "notify_failed":
        return f"notify_failed kind={e.get('kind')} inc={e.get('incident_id')} error={e.get('error')}"
    if kind == "incident":
        return (f"incident action={e.get('action')} inc={e.get('incident_id')} "
                f"state={e.get('state')} trigger={e.get('trigger')}")
    if kind == "probe":
        return f"probe    ok={e.get('ok')} firing={e.get('firing')} detail={e.get('detail')}"
    return None


def loadgen_running() -> bool:
    p = subprocess.run(["docker", "ps", "-q", "--filter", f"name={CONTAINER}"],
                       capture_output=True, text=True, check=False)
    return bool(p.stdout.strip())


def cleanup() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, text=True, check=False)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def observe(start: datetime, fault_sec: int, window_sec: int, resolve_windows: int,
            fault_end: datetime | None = None) -> tuple[dict, datetime | None]:
    """RESOLVED 알림이 오거나 대기 한도가 지날 때까지 로그를 읽는다. (분석 결과, 실제 주입 종료 시각)을 돌려준다.

    fault_end를 미리 알고 있으면(stop 시나리오) 넘긴다. 없으면 loadgen 컨테이너가 사라진 시각으로 잡는다.
    """
    since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    wait_after_fault = (resolve_windows + 2) * window_sec
    printed: set[str] = set()
    result = analyze([])
    tick = 0
    while True:
        time.sleep(1)
        tick += 1
        if fault_end is None and not loadgen_running():
            fault_end = datetime.now(UTC)
            log(f"[T+{(fault_end - start).total_seconds():>4.0f}s] loadgen 종료 (주입 종료)")
        if tick % POLL_SEC:
            continue

        events = watcher_events("--since", since)
        for e in events:
            key = json.dumps({k: v for k, v in e.items() if k != "_dt"}, sort_keys=True)
            text = describe(e) if e["event"] in SHOWN_EVENTS else None
            if text and key not in printed:
                printed.add(key)
                log(f"[T+{max(0, (e['_dt'] - start).total_seconds()):>4.0f}s] {text}")
        result = analyze(events)

        if result["RESOLVED"] or fault_end is None:
            if result["RESOLVED"]:
                return result, fault_end
            continue
        elapsed = (datetime.now(UTC) - fault_end).total_seconds()
        if result["incident_id"] is None and elapsed > 2 * window_sec:
            log("[주의] 주입이 끝난 뒤에도 장애가 열리지 않아 관찰을 마칩니다.")
            return result, fault_end
        if elapsed > wait_after_fault:
            log(f"[주의] 주입 종료 후 {wait_after_fault}초 안에 RESOLVED 알림이 오지 않아 관찰을 마칩니다.")
            return result, fault_end


def judge(result: dict, start: datetime, fault_end: datetime | None, fault_sec: int, window_sec: int,
          resolve_windows: int, quick: bool, detect_limit: int = DETECT_LIMIT_SEC, scenario: str = "errors") -> int:
    limit_c = (resolve_windows + 2) * window_sec
    rows = []

    firing = result["FIRING"]
    if firing:
        detect = (firing[0]["_dt"] - start).total_seconds()
        rows.append(("A", detect <= detect_limit, f"첫 FIRING 알림까지 {detect:.0f}초 (한도 {detect_limit}초)"))
    else:
        rows.append(("A", False, "FIRING 알림이 오지 않음"))

    rows.append(("B", len(firing) == 1, f"FIRING 알림 {len(firing)}건 (기대 1건)"))

    resolved = result["RESOLVED"]
    if resolved and fault_end is not None:
        gap = (resolved[0]["_dt"] - fault_end).total_seconds()
        rows.append(("C", gap <= limit_c, f"주입 종료 → RESOLVED 알림 {gap:.0f}초 (한도 {limit_c}초)"))
    else:
        rows.append(("C", False, f"RESOLVED 알림이 오지 않음 (한도 {limit_c}초)"))

    if scenario == "stop":
        updates = result["UPDATE"]
        rows.append(("D", not updates, f"UPDATE 알림 {len(updates)}건 (기대 0건)"))

    log()
    log("== 결과 ==")
    overall = True
    for item, ok, text in rows:
        if quick and item == "B":
            log(f"{item}  참고({'PASS' if ok else 'FAIL'})  {text}  [--quick: 판정에서 제외]")
            continue
        overall &= ok
        log(f"{item}  {'PASS' if ok else 'FAIL'}  {text}")
    injected = f"{(fault_end - start).total_seconds():.0f}초" if fault_end else "측정 못 함"
    log(f"장애 id: {result['incident_id']} · 주입 계획 {fault_sec}초 / 실측 {injected} · "
        f"UPDATE 알림 {len(result['UPDATE'])}건 · 알림 전송 실패 {len(result['failed'])}건")
    for e in result["failed"]:
        log(f"  notify_failed kind={e.get('kind')} error={e.get('error')}")
    if scenario == "stop":
        log(f"R4 즉시 장애: {'예' if result['r4_immediate'] else '아니오'} (참고, 판정에 영향 없음)")
    if fault_end and (fault_end - start).total_seconds() < fault_sec * 0.9:
        log("[주의] 주입이 계획보다 일찍 끝났습니다. loadgen 로그를 확인하세요.")
    log(f"전체: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


def run_stop_scenario(fault_sec: int, window_sec: int, resolve_windows: int) -> int:
    """앱을 fault_sec 동안 멈췄다가 되살린다. 로그가 끊기므로 R4(합성 프로브) 경로로 장애가 열려야 한다."""
    log(f"시험 시작: 앱 정지 {fault_sec}초 (docker compose stop app → start app), window {window_sec}초, "
        f"resolve_windows {resolve_windows}")
    signal.signal(signal.SIGTERM, _raise_interrupt)
    start = datetime.now(UTC)                    # 주입 시작 = stop 명령 직전
    try:
        log(f"[T+   0s] {start.isoformat(timespec='seconds')} docker compose stop app")
        stop = compose("stop", "app")
        if stop.returncode != 0:
            log(f"app 정지 실패: {stop.stderr.strip()[:300]}")
            return 2
        log(f"[T+{(datetime.now(UTC) - start).total_seconds():>4.0f}s] app 정지 완료, {fault_sec}초 동안 유지")

        hold_until = time.monotonic() + fault_sec
        while time.monotonic() < hold_until:
            time.sleep(min(30.0, max(0.0, hold_until - time.monotonic())))
            log(f"[T+{(datetime.now(UTC) - start).total_seconds():>4.0f}s] 앱 정지 유지 중")

        up = compose("start", "app")
        fault_end = datetime.now(UTC)            # 주입 종료 = start 명령 직후
        if up.returncode != 0:
            log(f"app 시작 실패: {up.stderr.strip()[:300]}")
            return 2
        log(f"[T+{(fault_end - start).total_seconds():>4.0f}s] docker compose start app 완료 (주입 종료)")

        result, _ = observe(start, fault_sec, window_sec, resolve_windows, fault_end)
    except KeyboardInterrupt:
        log("\n중단됨: app을 다시 시작합니다.")
        return 130
    finally:
        restart = compose("start", "app")        # 어떤 경로로 끝나도 앱이 멈춘 채 남지 않게 한다
        if restart.returncode != 0:
            log(f"[경고] app 시작 실패, 직접 실행하세요: docker compose start app ({restart.stderr.strip()[:200]})")
    return judge(result, start, fault_end, fault_sec, window_sec, resolve_windows, False,
                 detect_limit=STOP_DETECT_LIMIT_SEC, scenario="stop")


def main() -> int:
    ap = argparse.ArgumentParser(description="v0 합격 시험: 알림 A(탐지 시간)·B(중복 없음)·C(복구 알림)")
    ap.add_argument("--scenario", choices=("errors", "stop"), default="errors",
                    help="errors: loadgen 5xx 주입(기본) / stop: 앱 정지로 R4 프로브 경로 시험")
    ap.add_argument("--fault-sec", type=int, default=None, help="주입(정지) 시간(초), 기본 errors 600 / stop 120")
    ap.add_argument("--quick", action="store_true", help="errors는 주입 120초로 줄인 빠른 확인용. B는 참고로만 표시")
    args = ap.parse_args()
    if args.scenario == "stop" and args.quick:
        ap.error("--quick은 errors 시나리오 전용입니다")
    fault_sec = args.fault_sec or (120 if args.quick or args.scenario == "stop" else 600)

    window_sec, resolve_windows = load_params()
    problems = check_preconditions(window_sec)
    if problems:
        log("사전 조건 불충족 - 시험을 시작하지 않습니다:")
        for p in problems:
            log(f"  - {p}")
        return 2

    if args.scenario == "stop":
        return run_stop_scenario(fault_sec, window_sec, resolve_windows)

    log(f"시험 시작: 에러 주입 {fault_sec}초 (rps 3, error-rate 1.0), window {window_sec}초, "
        f"resolve_windows {resolve_windows}{', --quick' if args.quick else ''}")
    signal.signal(signal.SIGTERM, _raise_interrupt)
    cleanup()                                    # 이전 실행이 남긴 같은 이름의 컨테이너만 정리
    start = datetime.now(UTC)
    run = compose("run", "--rm", "-d", "--name", CONTAINER, "loadgen",
                  "--rps", "3", "--error-rate", "1.0", "--duration", str(fault_sec))
    if run.returncode != 0:
        log(f"loadgen 실행 실패: {run.stderr.strip()[:300]}")
        return 2
    log(f"[T+   0s] {start.isoformat(timespec='seconds')} 에러 주입 시작")

    try:
        result, fault_end = observe(start, fault_sec, window_sec, resolve_windows)
    except KeyboardInterrupt:
        log("\n중단됨: loadgen 컨테이너를 정리합니다.")
        return 130
    finally:
        cleanup()
    return judge(result, start, fault_end, fault_sec, window_sec, resolve_windows, args.quick)


if __name__ == "__main__":
    sys.exit(main())
