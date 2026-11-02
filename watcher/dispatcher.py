"""알림 전송과 LLM 분석을 감시 루프 밖(워커 스레드)에서 처리한다. 표준 라이브러리만.

감시 루프(메인 스레드) ──submit()──▶ send_queue(200) ──▶ 알림 스레드: notifier.send (재시도 등 기존 동작 그대로)
                                            │ FIRING을 보낸 뒤(성공이든 실패든)
                                            ▼
                                      llm_queue(20) ──▶ LLM 스레드: 프롬프트 → Ollama → 파싱 → ANALYSIS를 send_queue로

- submit은 어떤 경우에도 즉시 돌아온다(블록 없음). 큐가 가득 차면 가장 오래된 UPDATE를 버리고,
  그래도 자리가 없으면 새 항목을 버린다. FIRING과 RESOLVED는 큐 안에서 버리지 않는다.
- notifier는 알림 스레드에서만 부른다. LLM 스레드는 DB·장애 상태를 읽지 않고 메인 스레드가 만든 근거(evidence)만 쓴다.
- 분석은 FIRING에만, 장애 하나당 한 번만 한다.
"""
import copy
import hashlib
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import analysis
import llm_client
import llm_trace

SEND_QUEUE_SIZE = 200
LLM_QUEUE_SIZE = 20
SEEN_IDS_MAX = 1000                     # 이미 분석한·복구된·실패 알림을 보낸 장애 id를 기억하는 개수 상한
WARMUP_TIMEOUT_SEC = 120                # 콜드 스타트(모델 적재) 상한. 분석 호출의 30초와 별개
WARMUP_PROMPT = "안녕"
CLOSE_TIMEOUT_SEC = 8.0                 # 종료 때 알림 큐를 비우며 기다리는 상한(compose stop_grace_period 15s 안)
CLOSE_POLL_SEC = 0.05
FOREIGN_NOTE = "주의: 한국어 외 문자가 섞였습니다."


@dataclass(frozen=True)
class IncidentRef:
    """후속 메시지 제목·꼬리말에 필요한 최소 정보. LLM 스레드가 장애 상태를 읽지 않도록 근거에서 만든다."""
    id: int
    service: str
    severity: str


@dataclass
class SendItem:
    kind: str
    inc: object                         # FIRING·UPDATE·RESOLVED는 Incident 복사본, ANALYSIS*는 IncidentRef
    ctx: dict = field(default_factory=dict)
    evidence: dict | None = None        # FIRING에만. 분석 요청에 그대로 넘긴다
    trigger: str = "window"
    trace: dict | None = None           # ANALYSIS*에만. 보낸 뒤 notify 단계를 채워 기록한다
    trace_t0: float = 0.0


@dataclass(frozen=True)
class LlmRequest:
    incident_id: int
    trigger: str
    evidence: dict


class _SendQueue(queue.Queue):
    """가득 차면 가장 오래된 UPDATE를 빼고 넣는 큐. queue.Queue가 허용하는 확장 방식(mutex·queue 속성)을 쓴다."""

    def offer(self, item: SendItem) -> tuple[bool, SendItem | None]:
        """즉시 돌아온다. (넣었는가, 대신 버린 UPDATE)."""
        with self.mutex:
            dropped = None
            if 0 < self.maxsize <= self._qsize():
                dropped = next((x for x in self.queue if x.kind == "UPDATE"), None)
                if dropped is None:
                    return False, None
                self.queue.remove(dropped)
                self.unfinished_tasks -= 1
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
            return True, dropped


class _Seen:
    """크기가 제한된 id 집합(오래된 것부터 잊는다). 여러 스레드에서 쓴다."""

    def __init__(self, limit: int = SEEN_IDS_MAX):
        self._d: OrderedDict = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, key) -> bool:
        """새로 넣었으면 True, 이미 있었으면 False."""
        with self._lock:
            if key in self._d:
                return False
            self._d[key] = None
            while len(self._d) > self._limit:
                self._d.popitem(last=False)
            return True

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._d

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)


def options_from(cfg, **override) -> llm_client.LlmOptions:
    """설정의 샘플링 옵션 7개를 모두 담는다(빠뜨리면 LlmOptions가 TypeError)."""
    opts = {"temperature": cfg.llm_temperature, "seed": cfg.llm_seed, "top_k": cfg.llm_top_k,
            "top_p": cfg.llm_top_p, "repeat_penalty": cfg.llm_repeat_penalty,
            "num_predict": cfg.llm_num_predict, "num_ctx": cfg.llm_num_ctx}
    opts.update(override)
    return llm_client.LlmOptions(**opts)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ms_since(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 3)


class Dispatcher:
    def __init__(self, cfg, notifier, emit, runbooks_text: str = "",
                 chat=llm_client.chat, version=llm_client.version):
        if cfg.llm_hold_sec > 0:
            raise ValueError(f"LLM_HOLD_SEC={cfg.llm_hold_sec!r}: LLM_HOLD_SEC>0은 아직 지원하지 않는다")
        self.cfg, self.notifier, self.emit, self.runbooks_text = cfg, notifier, emit, runbooks_text
        self._chat, self._version = chat, version
        self.llm_enabled = bool(cfg.llm_enabled)
        self.options = options_from(cfg)
        self.send_queue = _SendQueue(SEND_QUEUE_SIZE)
        self.llm_queue = queue.Queue(LLM_QUEUE_SIZE) if self.llm_enabled else None   # 꺼져 있으면 만들지 않는다
        self._analyzed = _Seen()        # 분석 요청을 넣은 장애 (알림 스레드만 쓴다)
        self._resolved = _Seen()        # RESOLVED를 접수한 장애 (메인 스레드가 쓰고 LLM 스레드가 읽는다)
        self._failed = _Seen()          # ANALYSIS_FAILED를 보낸 장애 (LLM 스레드만 쓴다)
        self.ollama_version: str | None = None
        self._threads: dict[str, threading.Thread] = {}
        self.warmup_thread: threading.Thread | None = None
        self._closing = False

    # ------------------------------------------------------------ 시작·감시

    def _targets(self) -> dict:
        targets = {"notify": self._send_loop}
        if self.llm_enabled:
            targets["llm"] = self._llm_loop
        return targets

    def _spawn(self, name: str) -> None:
        t = threading.Thread(target=self._targets()[name], name=f"watcher-{name}", daemon=True)
        self._threads[name] = t
        t.start()

    def start(self, warmup: bool = True) -> None:
        for name in self._targets():
            self._spawn(name)
        if self.llm_enabled and warmup:
            self.warmup_thread = threading.Thread(target=self._warmup, name="watcher-warmup", daemon=True)
            self.warmup_thread.start()

    def ensure_alive(self) -> None:
        """죽은 워커 스레드를 다시 띄운다. 메인 루프가 매 틱 부른다. 큐에 남은 항목은 새 스레드가 이어서 처리한다."""
        for name, t in list(self._threads.items()):
            if not t.is_alive():
                self._spawn(name)
                self.emit("worker_restarted", worker=name)

    @property
    def threads(self) -> dict:
        return dict(self._threads)

    # ------------------------------------------------------------ 종료

    def close(self, timeout: float = CLOSE_TIMEOUT_SEC) -> dict:
        """종료 직전에 부른다. 알림 큐가 빌 때까지(보내는 중인 것 포함) 최대 timeout초 기다린다.
        분석 요청(llm_queue)은 기다리지 않는다(사라져도 된다). 두 번째 호출은 기다리지 않고 지금 상태만 돌려준다."""
        if self._closing:                   # 신호가 겹쳐 핸들러 안에서 다시 불려도 막히지 않게 잠금 없이 표시만 본다
            return self._drain_state()
        self._closing = True
        deadline = time.monotonic() + timeout
        while self._unfinished() and time.monotonic() < deadline:
            time.sleep(CLOSE_POLL_SEC)
        result = self._drain_state()
        self.emit("shutdown_drain", **result)
        return result

    def _unfinished(self) -> int:
        with self.send_queue.mutex:
            return self.send_queue.unfinished_tasks

    def _drain_state(self) -> dict:
        remaining = self._unfinished()
        return {"drained": remaining == 0, "remaining": remaining,
                "llm_pending": self.llm_queue.qsize() if self.llm_queue is not None else 0}

    # ------------------------------------------------------------ 메인 스레드가 부르는 곳

    def submit(self, kind: str, inc, evidence: dict | None = None, trigger: str = "window", **ctx) -> None:
        """알림 하나를 큐에 넣고 즉시 돌아온다. 넣는 값은 복사해 메인 스레드가 나중에 바꿔도 영향이 없게 한다."""
        try:
            if kind == "RESOLVED":
                self._resolved.add(inc.id)              # 큐에 넣은 시점에 복구로 본다(분석 '복구 후 도착' 표시용)
            item = SendItem(kind, copy.deepcopy(inc), copy.deepcopy(ctx),
                            copy.deepcopy(evidence) if evidence is not None else None, trigger)
            self._offer(item)
        except Exception as e:                          # 감시 루프를 절대 막지 않는다
            self.emit("worker_error", worker="submit", kind=kind, error=type(e).__name__)

    # ------------------------------------------------------------ 알림 스레드

    def _offer(self, item: SendItem) -> bool:
        ok, dropped = self.send_queue.offer(item)
        if dropped is not None:
            self.emit("notify_dropped", kind=dropped.kind, incident_id=dropped.inc.id, reason="oldest_update",
                      queue_size=SEND_QUEUE_SIZE)
        if not ok:
            self.emit("notify_dropped", kind=item.kind, incident_id=item.inc.id, reason="queue_full",
                      queue_size=SEND_QUEUE_SIZE)
        return ok

    def _send_loop(self) -> None:
        while True:
            item = self.send_queue.get()
            try:
                self._deliver(item)
            except Exception as e:
                self.emit("worker_error", worker="notify", kind=item.kind, incident_id=item.inc.id,
                          error=type(e).__name__)
            finally:
                self.send_queue.task_done()

    def _deliver(self, item: SendItem) -> None:
        sent = self.notifier.send(item.kind, item.inc, **item.ctx)      # 예외를 내지 않는다(notifier 계약)
        if item.trace is not None:
            self._finish_trace(item, sent)
        if item.kind == "FIRING" and self.llm_enabled:
            self._request_analysis(item)

    def _request_analysis(self, item: SendItem) -> None:
        inc_id = item.inc.id
        if item.evidence is None:
            self.emit("llm_skipped", incident_id=inc_id, reason="no_evidence")
            return
        if not self._analyzed.add(inc_id):
            self.emit("llm_skipped", incident_id=inc_id, reason="already_analyzed")
            return
        try:
            self.llm_queue.put_nowait(LlmRequest(inc_id, item.trigger, item.evidence))
        except queue.Full:
            self.emit("llm_skipped", incident_id=inc_id, reason="queue_full")

    # ------------------------------------------------------------ LLM 스레드

    def _llm_loop(self) -> None:
        while True:
            req = self.llm_queue.get()
            try:
                self._analyze(req)
            except Exception as e:
                self.emit("worker_error", worker="llm", incident_id=req.incident_id, error=type(e).__name__)
            finally:
                self.llm_queue.task_done()

    def _analyze(self, req: LlmRequest) -> None:
        cfg, ev = self.cfg, req.evidence
        ref = IncidentRef(req.incident_id, cfg.service, ev.get("incident", {}).get("severity", "warning"))
        stages = [llm_trace.make_stage("evidence", "ok", None,
                                       {"*": "computed", "top_error_kinds": "measured", "top_paths": "measured",
                                        "probe": "measured"}, ev)]
        self.emit("llm_start", incident_id=req.incident_id, trigger=req.trigger)

        t0 = time.monotonic()
        messages = analysis.build_messages(ev, self.runbooks_text)
        system, user = messages[0]["content"], messages[1]["content"]
        est_tokens = analysis.estimate_tokens(system) + analysis.estimate_tokens(user)
        stages.append(llm_trace.make_stage(
            "prompt", "ok", _ms_since(t0),
            {"*": "computed", "version": "input", "user": "input"},
            {"version": analysis.PROMPT_VERSION, "system_sha256": _sha256(system), "system_chars": len(system),
             "user": user, "est_tokens": est_tokens}))

        result = self._chat(cfg.ollama_url, cfg.llm_model, messages, self.options, fmt=analysis.ANALYSIS_SCHEMA,
                            keep_alive=cfg.llm_keep_alive, connect_timeout=cfg.llm_connect_timeout,
                            total_timeout=cfg.llm_timeout)
        stages.append(llm_trace.llm_call_stage(result))

        parsed = analysis.parse_answer(result.text) if result.ok else None
        if parsed is None:
            stages.append(llm_trace.make_stage("parse", "skipped", None, {"*": "computed"},
                                               {"ok": False, "flags": []}, [f"호출 실패: {result.reason}"]))
        else:
            stages.append(llm_trace.make_stage("parse", "ok" if parsed.ok else "error", None, {"*": "computed"},
                                               {"ok": parsed.ok, "reason": parsed.reason or None,
                                                "flags": parsed.flags}))

        late = req.incident_id in self._resolved
        if parsed is not None and parsed.ok:
            self.emit("llm_done", incident_id=req.incident_id, wall_ms=result.wall_ms, flags=parsed.flags,
                      prompt_tokens=result.prompt_eval_count, gen_tokens=result.eval_count, late=late)
            text = analysis.render_slack_text(parsed, cfg.llm_model, result.wall_ms / 1000,
                                              result.prompt_eval_count or est_tokens, result.eval_count or 0)
            note = self._flags_line(parsed.flags)
            if note:
                text += "\n" + note
            item = SendItem("ANALYSIS", ref, {"text": analysis.mask(text), "late": late}, trigger=req.trigger)
            reason = None
        else:
            reason = result.reason if parsed is None else parsed.reason
            self.emit("llm_failed", incident_id=req.incident_id, reason=reason, wall_ms=result.wall_ms)
            if not self._failed.add(req.incident_id):    # 그 장애의 첫 실패만 알린다
                self._write_trace(req, stages, reason, "skipped", {"sent": False}, ["이미 실패 알림을 보냄"])
                return
            item = SendItem("ANALYSIS_FAILED", ref, {"text": f"AI 분석 실패({reason})", "late": late},
                            trigger=req.trigger)

        item.trace = {"req": req, "stages": stages, "error": reason}
        item.trace_t0 = time.monotonic()
        if not self._offer(item):
            self._write_trace(req, stages, reason or "notify_dropped", "error",
                              {"sent": False, "kind": item.kind}, ["알림 큐가 가득 차 버림"])

    @staticmethod
    def _flags_line(flags: list) -> str:
        parts = [FOREIGN_NOTE] if "foreign_script" in flags else []
        others = [f for f in flags if f != "foreign_script"]
        if others:
            parts.append("표시: " + ", ".join(others))
        return " ".join(parts)

    # ------------------------------------------------------------ 기록

    def _finish_trace(self, item: SendItem, sent: bool) -> None:
        """알림 스레드에서 ANALYSIS*를 보낸 뒤 notify 단계를 채워 한 줄 기록한다."""
        t = item.trace
        error = t["error"] if t["error"] is not None else (None if sent else "notify_failed")
        self._write_trace(t["req"], t["stages"], error, "ok" if sent else "error",
                          {"sent": sent, "kind": item.kind, "mode": "webhook" if self.notifier.url else "dryrun"},
                          [], duration_ms=_ms_since(item.trace_t0))

    def _write_trace(self, req: LlmRequest, stages: list, error: str | None, notify_status: str, notify_out: dict,
                     notes: list, duration_ms: float | None = None) -> None:
        stages = [*stages, llm_trace.make_stage("notify", notify_status, duration_ms, {"*": "measured"},
                                                notify_out, notes)]
        rec = llm_trace.build_run_record(
            req.incident_id, req.trigger, f"incident #{req.incident_id:04d} {req.trigger}", self.cfg.llm_model,
            analysis.PROMPT_VERSION, self.options, stages, "ok" if error is None else "error", error,
            self.ollama_version)
        llm_trace.append_run(self.cfg.llm_trace_path, rec)

    # ------------------------------------------------------------ 예열

    def _warmup(self) -> None:
        """Ollama 재시작 직후 첫 호출은 모델 적재로 수십 초가 걸린다. 짧은 호출로 미리 적재한다. 실패해도 무시."""
        try:
            cfg = self.cfg
            self.ollama_version = self._version(cfg.ollama_url, timeout=cfg.llm_connect_timeout)
            res = self._chat(cfg.ollama_url, cfg.llm_model, [{"role": "user", "content": WARMUP_PROMPT}],
                             options_from(cfg, num_predict=1), keep_alive=cfg.llm_keep_alive,
                             connect_timeout=cfg.llm_connect_timeout, total_timeout=WARMUP_TIMEOUT_SEC)
            if res.ok or res.reason == "truncated":     # num_predict 1이라 done_reason=length(truncated)가 정상이다
                self.emit("warmup_done", wall_ms=res.wall_ms, load_ms=res.load_ms, ollama_version=self.ollama_version)
            else:
                self.emit("warmup_failed", reason=res.reason, wall_ms=res.wall_ms)
        except Exception as e:
            self.emit("warmup_failed", reason=f"internal_error:{type(e).__name__}")
