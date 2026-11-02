import json
import signal
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

import analysis
from config import Config
from dispatcher import Dispatcher
from logreader import LogReader
from notifier import Notifier
from policy import should_notify_update, should_open_from_probe, should_update_baseline
from probe import Prober
from rules import evaluate, probe_finding
from state import IncidentManager, Store
from stats import WindowStats, summarize

RUNBOOK_DIR = Path(__file__).resolve().parent / "runbooks"
_emit_lock = threading.RLock()     # 워커 스레드도 emit을 부른다. RLock: SIGTERM 핸들러가 출력 도중 끼어들어도 교착 없음


def emit(event: str, **fields) -> None:
    """Watcher 자신의 동작도 JSON 한 줄로 남긴다."""
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False)
    with _emit_lock:
        print(line, flush=True)


def make_evidence(cfg: Config, *args, **kwargs) -> dict | None:
    """LLM 분석용 근거(메인 스레드에서 만든다). LLM이 꺼져 있거나 만들다 실패하면 None. 감시 루프를 막지 않는다."""
    if not cfg.llm_enabled:
        return None
    try:
        return analysis.build_evidence(*args, window_sec=cfg.window_sec, exclude_paths=cfg.exclude_paths, **kwargs)
    except Exception as e:
        emit("llm_skipped", reason=f"evidence_error:{type(e).__name__}")
        return None


def main() -> None:
    dispatcher = None                                               # 핸들러가 Dispatcher 생성 전에 불릴 수도 있다

    def _stop(signum, frame):
        if dispatcher is not None:
            dispatcher.close()                                      # 큐에 남은 알림을 최대 8초 보내고 끝낸다
        emit("stop", signal=signal.Signals(signum).name)
        sys.exit(0)
    # docker stop / restart 시 즉시 정상 종료 (없으면 PID 1이 SIGTERM을 무시해 10초 뒤 SIGKILL, exit 137)
    signal.signal(signal.SIGTERM, _stop)

    cfg = Config()
    store = Store(cfg.state_path)
    manager = IncidentManager(store, cfg.service, cfg.resolve_windows)
    notifier = Notifier(cfg.notify_provider, cfg.webhook_url, emit, llm_enabled=cfg.llm_enabled)
    runbooks_text = analysis.load_runbooks(RUNBOOK_DIR)            # 시작할 때 한 번 읽는다
    dispatcher = Dispatcher(cfg, notifier, emit, runbooks_text)    # LLM_HOLD_SEC>0이면 여기서 멈춘다
    prober = Prober(cfg.probe_url, cfg.probe_timeout, cfg.probe_fail_threshold)

    # 재시작 전 상태 복원: 읽던 위치, 기준선 기록, 마지막으로 읽은 시각을 한 묶음으로
    ckpt = store.get_meta("checkpoint", {})
    reader = LogReader(cfg.log_path)
    reader.restore(ckpt.get("reader", {}))
    history: deque[float] = deque(ckpt.get("history", []), maxlen=cfg.r3_baseline_windows)
    last_read_at = ckpt.get("last_read_at")                         # epoch 초, 없으면 None

    ticks_per_window = max(1, round(cfg.window_sec / cfg.probe_interval))   # 60 / 15 = 4
    open_inc = store.open_incident(cfg.service)
    emit("start",
         window_sec=cfg.window_sec, probe_interval=cfg.probe_interval, ticks_per_window=ticks_per_window,
         resolve_windows=cfg.resolve_windows, restored_offset=reader.offset, restored_history=len(history),
         open_incident=open_inc.id if open_inc else None,
         notify_mode=cfg.notify_provider if cfg.webhook_url else "dryrun",
         llm_enabled=cfg.llm_enabled, llm_model=cfg.llm_model if cfg.llm_enabled else None)
    dispatcher.start()                                              # 알림 스레드(+ LLM 스레드, 예열)

    tick = 0
    next_tick = time.monotonic() + cfg.probe_interval
    while True:
        time.sleep(max(0, next_tick - time.monotonic()))
        next_tick += cfg.probe_interval                             # 예정 시각 기준, 지연 누적 방지
        tick += 1
        dispatcher.ensure_alive()                                   # 죽은 워커 스레드가 있으면 다시 띄운다

        # A. 합성 프로브 (매 틱, 15초)
        was_firing = prober.firing
        p = prober.probe()
        if not p["ok"] or was_firing != prober.firing:            # 실패하거나 상태가 바뀔 때만 기록
            emit("probe", **p, firing=prober.firing)

        if tick % ticks_per_window != 0:
            # 창 사이: R4가 막 켜졌고 열린 장애가 없으면 창을 기다리지 않고 바로 장애를 연다
            if should_open_from_probe(was_firing, prober.firing, store.open_incident(cfg.service)):
                action, inc, _ = manager.step([probe_finding(True, prober)])
                emit("incident", action=action, incident_id=inc.id, state=inc.state, trigger="R4")
                baseline = median(history) if len(history) >= cfg.r3_min_windows else None
                evidence = make_evidence(cfg, inc, WindowStats(0, 0, 0.0, 0.0), None, baseline, p, time.time(),
                                         current_rules=["R4"], runbooks_text=runbooks_text)
                dispatcher.submit("FIRING", inc, evidence=evidence, trigger="R4", probe=p)
            continue

        # B. 로그 창 처리 (4번째 틱마다, 60초)
        # 1. 새 로그 읽기 + 실제 읽은 기간 기준으로 집계
        records, bad = reader.read_new()
        now = time.time()
        elapsed = now - last_read_at if last_read_at else cfg.window_sec
        last_read_at = now
        stats = summarize(records, max(elapsed, 1), cfg.exclude_paths)

        # 2. 규칙 판정 (기준선은 과거 NORMAL 창들의 중앙값, 현재 창은 제외 / R4는 이 창 동안 한 번이라도 켜졌는가)
        baseline = median(history) if len(history) >= cfg.r3_min_windows else None
        findings = evaluate(stats, baseline, cfg) + [probe_finding(prober.take_window_flag(), prober)]
        fired = [f for f in findings if f.fired]

        # 3. 장애 상태 전이
        action, inc, added = manager.step(fired)

        emit("window",
             requests=stats.requests,
             errors_5xx=stats.errors_5xx,
             error_rate=round(stats.error_rate, 3),
             p95_ms=round(stats.p95_ms, 1),
             rpm=round(stats.rpm, 1),
             baseline_rpm=None if baseline is None else round(baseline, 1),
             baseline_n=len(history),
             elapsed_sec=round(elapsed, 1),
             fired=[f.rule_id for f in fired],
             details={f.rule_id: f.detail for f in findings},
             bad_lines=bad,
             action=action,
             added_rules=added,
             incident_id=inc.id if inc else None,
             state=inc.state if inc else "NORMAL")

        # 4. 알림은 새 장애(fire), 유예 시간이 지난 신호 추가(escalate), 장애 종료(resolve) 세 전이에서만
        #    (보내기는 알림 스레드가 한다. submit은 큐에 넣고 바로 돌아온다)
        if action == "fire":
            evidence = make_evidence(cfg, inc, stats, records, baseline, p, now,
                                     current_rules=[f.rule_id for f in fired], runbooks_text=runbooks_text)
            dispatcher.submit("FIRING", inc, evidence=evidence, stats=stats, baseline=baseline, probe=p)
        elif should_notify_update(action, inc, cfg.update_grace_sec):
            dispatcher.submit("UPDATE", inc, stats=stats, baseline=baseline, probe=p, added=added)
        elif action == "resolve":
            dispatcher.submit("RESOLVED", inc)

        # 5. 열린 장애가 없는 NORMAL 창이고 표본이 충분할 때만 기준선에 반영
        if should_update_baseline(action, stats.requests, cfg.min_requests):
            history.append(stats.rpm)

        # 6. 상태를 한 번에 저장 (원자적으로 기록되어 반쯤 저장된 상태가 생기지 않음)
        store.set_meta("checkpoint", {
            "reader": reader.snapshot(),
            "history": list(history),
            "last_read_at": last_read_at,
        })


if __name__ == "__main__":
    main()
