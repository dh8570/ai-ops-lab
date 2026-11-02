"""LLM 호출 기록(실행 기록 형식 v1, kind "incident"). 표준 라이브러리만.

레코드 하나를 JSON 한 줄로 llm_runs.jsonl에 이어 쓴다. 모양은 탐험 사이트의 실행 기록 v1과 같다.
기록 실패는 표준 오류에만 남기고 예외를 내지 않는다(감시 루프를 막지 않기 위해).
"""
import json
import math
import os
import re
import secrets
import sys
import threading
from datetime import UTC, datetime

from llm_client import LlmOptions, LlmResult

SCHEMA_VERSION = 1
EVIDENCE_GRADES = ("measured", "computed", "illustrative", "unverified", "input")
STAGE_IDS = ("evidence", "prompt", "llm_call", "parse", "notify")
STAGE_STATUSES = ("ok", "error", "skipped")
RUN_STATUSES = ("ok", "error", "aborted")
SETTING_KEYS = ("temperature", "seed", "top_k", "top_p", "repeat_penalty", "num_predict", "num_ctx")
RUN_ID_RE = re.compile(r"r-\d{8}-\d{6}-[0-9a-f]{4}")
CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
MAX_RECORD_BYTES = 2 * 1024 * 1024      # 레코드 하나의 직렬화 상한
MAX_FILE_BYTES = 20 * 1024 * 1024       # 이를 넘으면 path.1로 돌린다
REDACTION_NOTE = "마스킹 단계 없음(A1-T1). 장애 데이터가 그대로 들어갈 수 있으므로 공개 금지"

_write_lock = threading.Lock()
_STAT_FIELDS = ("load_ms", "prompt_eval_count", "prompt_eval_cached_count", "prompt_eval_ms",
                "eval_count", "eval_ms", "total_ms", "wall_ms")


def make_stage(id: str, status: str, duration_ms: float | None, evidence: dict, output: dict, notes=()) -> dict:
    return {"id": id, "status": status, "duration_ms": duration_ms, "evidence": dict(evidence),
            "output": dict(output), "notes": list(notes)}


def llm_call_stage(result: LlmResult) -> dict:
    """llm_call 단계. 측정값은 measured, 계산값은 computed, 보낸 설정은 input. 값이 없으면 unverified."""
    req = result.request or {}
    out = {"ok": result.ok, "reason": result.reason, "http_status": result.http_status,
           "done_reason": result.done_reason, "text": result.text,
           **{k: getattr(result, k) for k in _STAT_FIELDS},
           "model": req.get("model"), "keep_alive": req.get("keep_alive"), "options": req.get("options")}
    computed = {
        "tok_s": (round(result.eval_count / (result.eval_ms / 1000), 2)
                  if result.eval_count and result.eval_ms else None),
        "outside_ollama_ms": None if result.total_ms is None else round(result.wall_ms - result.total_ms, 3),
    }
    out.update(computed)
    evidence = {"*": "measured", "model": "input", "keep_alive": "input", "options": "input",
                **dict.fromkeys(computed, "computed")}
    for k in (*_STAT_FIELDS, *computed):
        if out[k] is None:
            evidence[k] = "unverified"
    notes = [] if result.ok else [f"실패: {result.reason}"]
    if result.ok and result.prompt_eval_cached_count is None:
        notes.append("Ollama가 prompt_eval_cached_count를 주지 않았다")
    return make_stage("llm_call", "ok" if result.ok else "error", result.wall_ms, evidence, out, notes)


def build_run_record(incident_id, trigger, title: str, model: str, prompt_version: str, options: LlmOptions,
                     stages: list, status: str, error: str | None, ollama_version: str | None,
                     now: datetime | None = None, rid: str | None = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid if rid is not None else f"r-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
        "created_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "kind": "incident",
        "title": title,
        "exportable": False,
        "redaction": {"applied": False, "note": REDACTION_NOTE},
        "environment": {"ollama_version": ollama_version, "model": model, "prompt_version": prompt_version},
        "settings": options.as_dict(),
        "status": status,
        "error": error,
        "stages": stages,
        "incident": {"id": incident_id, "trigger": trigger},    # v1 표에 없는 추가 필드(어느 장애의 분석인지)
    }


def _is_num(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _check_stage(i: int, st, seen: set, problems: list) -> None:
    p = f"stages[{i}]"
    if not isinstance(st, dict):
        problems.append(f"{p}: 객체가 아님")
        return
    if st.get("id") not in STAGE_IDS:
        problems.append(f"{p}.id: 알 수 없는 단계 {st.get('id')!r}")
    elif st["id"] in seen:
        problems.append(f"{p}.id: 중복 {st['id']!r}")
    else:
        seen.add(st["id"])
    if st.get("status") not in STAGE_STATUSES:
        problems.append(f"{p}.status: 허용되지 않은 값 {st.get('status')!r}")
    d = st.get("duration_ms")
    if d is not None and not (_is_num(d) and d >= 0):
        problems.append(f"{p}.duration_ms: 0 이상의 숫자 또는 null이어야 함")
    ev = st.get("evidence")
    if not isinstance(ev, dict) or "*" not in ev:
        problems.append(f"{p}.evidence: \"*\" 키가 있는 객체여야 함")
    else:
        for k, g in ev.items():
            if not isinstance(k, str) or g not in EVIDENCE_GRADES:
                problems.append(f"{p}.evidence[{k!r}]: 알 수 없는 등급 {g!r}")
    if not isinstance(st.get("output"), dict):
        problems.append(f"{p}.output: 객체여야 함")
    notes = st.get("notes")
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        problems.append(f"{p}.notes: 문자열 목록이어야 함")


def validate_record(rec) -> list[str]:
    """문제 목록(비어 있으면 통과)."""
    if not isinstance(rec, dict):
        return ["레코드가 객체가 아님"]
    problems: list[str] = []
    if rec.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version: 1이어야 함")
    rid = rec.get("run_id")
    if not (isinstance(rid, str) and RUN_ID_RE.fullmatch(rid)):
        problems.append(f"run_id: 형식 불일치 {rid!r}")
    ca = rec.get("created_at")
    if not (isinstance(ca, str) and CREATED_AT_RE.fullmatch(ca)):
        problems.append(f"created_at: 형식 불일치 {ca!r}")
    if rec.get("kind") != "incident":
        problems.append(f"kind: incident여야 함 {rec.get('kind')!r}")
    if not isinstance(rec.get("title"), str):
        problems.append("title: 문자열이어야 함")
    if rec.get("exportable") is not False:
        problems.append("exportable: 아직 false만 허용")
    red = rec.get("redaction")
    if not (isinstance(red, dict) and isinstance(red.get("applied"), bool) and isinstance(red.get("note"), str)):
        problems.append("redaction: {applied: bool, note: 문자열}이어야 함")
    env = rec.get("environment")
    if not (isinstance(env, dict) and isinstance(env.get("model"), str) and isinstance(env.get("prompt_version"), str)
            and (env.get("ollama_version") is None or isinstance(env.get("ollama_version"), str))):
        problems.append("environment: ollama_version(문자열|null), model, prompt_version이 필요")
    st = rec.get("settings")
    if not isinstance(st, dict) or set(st) != set(SETTING_KEYS) or not all(_is_num(v) for v in st.values()):
        problems.append(f"settings: 숫자인 {len(SETTING_KEYS)}개 옵션이 모두 있어야 함")
    status, error = rec.get("status"), rec.get("error")
    if status not in RUN_STATUSES:
        problems.append(f"status: 허용되지 않은 값 {status!r}")
    if not (error is None or isinstance(error, str)):
        problems.append("error: 문자열 또는 null이어야 함")
    elif status == "ok" and error is not None:
        problems.append("error: status가 ok이면 null이어야 함")
    elif status in ("error", "aborted") and error is None:
        problems.append("error: status가 ok가 아니면 이유가 필요")
    inc = rec.get("incident")
    if inc is not None and not (isinstance(inc, dict) and isinstance(inc.get("trigger"), str)):
        problems.append("incident: {id, trigger(문자열)}이어야 함")
    stages = rec.get("stages")
    if not isinstance(stages, list):
        problems.append("stages: 목록이어야 함")
    else:
        seen: set = set()
        for i, s in enumerate(stages):
            _check_stage(i, s, seen, problems)
    try:
        size = len(json.dumps(rec, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size > MAX_RECORD_BYTES:
            problems.append(f"직렬화 크기 {size}바이트가 상한 {MAX_RECORD_BYTES}를 넘음")
    except (TypeError, ValueError) as e:
        problems.append(f"JSON으로 직렬화할 수 없음: {e}")
    return problems


def append_run(path, rec, max_bytes: int = MAX_FILE_BYTES) -> bool:
    """검증 뒤 한 줄을 이어 쓴다. 파일이 max_bytes를 넘으면 path.1로 이름을 바꾼 뒤(기존 .1은 덮어씀) 새 파일에 쓴다.
    실패는 표준 오류에만 남기고 False를 돌려준다. 예외는 내지 않는다."""
    try:
        problems = validate_record(rec)
        if problems:
            print(f"llm_trace: 기록 검증 실패, 쓰지 않음: {'; '.join(problems[:5])}", file=sys.stderr)
            return False
        line = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path = os.fspath(path)
        with _write_lock:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
            with open(path, "ab") as f:
                f.write(line)
                f.flush()
        return True
    except Exception as e:
        print(f"llm_trace: 기록 실패(무시): {type(e).__name__}: {e}", file=sys.stderr)
        return False
