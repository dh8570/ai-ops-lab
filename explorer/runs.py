"""실행 기록(공통 기록 형식, schema_version 1). 실행 하나 = JSON 파일 하나. 화면은 이 파일만 읽는다.

위치: <DATA_DIR>/runs/<run_id>.json 과 <DATA_DIR>/index.json (DATA_DIR은 볼륨 explorer-traces, 컨테이너의 /data).
쓰기 규칙: 같은 폴더의 임시 파일에 쓰고 os.replace로 바꿔 넣어 반쯤 쓰인 파일이 읽히지 않게 한다.
기록 실패는 표준 오류에만 남기고 예외를 밖으로 내보내지 않는다(응답 스트림을 망치면 안 된다).
"""
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^r-\d{8}-\d{6}-[0-9a-f]{4}$")      # 서버와 화면(js/data.js)이 같은 식을 쓴다
CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
MAX_RECORD_BYTES = 2 * 1024 * 1024                          # 직렬화한 기록이 이보다 크면 쓰지 않는다
INDEX_MAX = 200                                             # index.json에는 최신순으로 최대 200개
# 증거 등급. input(입력)은 사용자가 정한 값(계산이나 측정이 아님). 허용 값만 늘었으므로 schema_version은 1 그대로다.
GRADES = ("measured", "computed", "illustrative", "unverified", "input")
RUN_STATUSES = ("ok", "error", "aborted")
STAGE_STATUSES = ("ok", "error", "skipped")
KINDS = ("playground", "incident")
PLAYGROUND_STAGES = ("input", "tokenize", "load", "prefill", "decode", "restore")
# 상태가 ok이고 증거가 미확인이 아닌 단계의 output에 꼭 있어야 하는 키
REQUIRED_OUTPUT = {
    "input": ("prompt", "prompt_chars", "prompt_bytes"),
    "tokenize": ("text_chars", "text_bytes", "nfc_changed", "token_count", "partial_count", "bytes_match", "tokens"),
    "load": ("load_ms",),
    "prefill": ("prompt_eval_count", "prompt_eval_ms", "local_token_count", "count_diff"),
    "decode": ("chunks", "chunk_count", "eval_count", "eval_ms", "tok_s", "done_reason", "first_chunk_ms"),
    "restore": ("server_joined", "final_response", "final_response_empty", "chars", "bytes"),
}
TOKEN_KEYS = ("index", "id", "piece", "hex", "partial", "special", "fragment_of")

_index_lock = threading.Lock()


def utc_iso(dt: datetime) -> str:
    """2026-10-28T06:45:12.345Z 모양(UTC, 밀리초)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def make_run_id(now: datetime | None = None, exists=lambda run_id: False, token=lambda: secrets.token_hex(2)) -> str:
    """'r-' + UTC 시각 + '-' + 16진수 4자. 같은 이름의 파일이 이미 있으면 다시 만든다."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    while True:
        run_id = f"r-{now:%Y%m%d-%H%M%S}-{token()}"
        if not exists(run_id):
            return run_id


# ---------------------------------------------------------------- 검증
def _is_num(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def validate_bundle(b) -> list[str]:
    """기록의 모양을 검사해 문제 목록을 돌려준다(비어 있으면 올바른 기록). 외부 라이브러리를 쓰지 않는다."""
    p: list[str] = []
    if not isinstance(b, dict):
        return ["기록이 객체가 아님"]

    def need(key, typ, label=None):
        if key not in b:
            p.append(f"필수 필드 없음: {key}")
            return False
        if not isinstance(b[key], typ) or (typ is int and isinstance(b[key], bool)):
            p.append(f"{label or key}의 형식이 올바르지 않음")
            return False
        return True

    if b.get("schema_version") != SCHEMA_VERSION:
        p.append(f"schema_version은 {SCHEMA_VERSION}이어야 함: {b.get('schema_version')!r}")
    if need("run_id", str) and not RUN_ID_RE.fullmatch(b["run_id"]):
        p.append(f"run_id 모양이 올바르지 않음: {b['run_id']!r}")
    if need("created_at", str) and not CREATED_AT_RE.fullmatch(b["created_at"]):
        p.append(f"created_at 모양이 올바르지 않음: {b['created_at']!r}")
    if need("kind", str) and b["kind"] not in KINDS:
        p.append(f"kind는 {list(KINDS)} 중 하나여야 함: {b['kind']!r}")
    need("title", str)
    need("exportable", bool)
    if need("redaction", dict):
        r = b["redaction"]
        if not isinstance(r.get("applied"), bool) or not isinstance(r.get("note"), str):
            p.append("redaction은 {applied: 참거짓, note: 문자열} 이어야 함")
    if need("environment", dict):
        e = b["environment"]
        if not isinstance(e.get("model"), str):
            p.append("environment.model이 문자열이 아님")
        if e.get("ollama_version") is not None and not isinstance(e.get("ollama_version"), str):
            p.append("environment.ollama_version은 문자열 또는 null")
        t = e.get("tokenizer")
        if not isinstance(t, dict) or not all(isinstance(t.get(k), str) for k in ("repo", "revision", "sha256")):
            p.append("environment.tokenizer는 {repo, revision, sha256} 문자열이어야 함")
    if need("settings", dict):
        s = b["settings"]
        for k in ("temperature", "num_predict", "num_ctx"):
            if not _is_num(s.get(k)):
                p.append(f"settings.{k}가 숫자가 아님")
        if "seed" in s and (not isinstance(s["seed"], int) or isinstance(s["seed"], bool)):
            p.append("settings.seed는 정수(안 줬으면 키 자체를 생략)")
    if need("status", str) and b["status"] not in RUN_STATUSES:
        p.append(f"status는 {list(RUN_STATUSES)} 중 하나여야 함: {b['status']!r}")
    if "error" not in b:
        p.append("필수 필드 없음: error")
    elif b["error"] is not None and not isinstance(b["error"], str):
        p.append("error는 문자열 또는 null")
    if need("stages", list):
        seen = set()
        for i, st in enumerate(b["stages"]):
            if not isinstance(st, dict):
                p.append(f"stages[{i}]가 객체가 아님")
                continue
            sid = st.get("id")
            if not isinstance(sid, str) or not sid:
                p.append(f"stages[{i}].id가 비어 있거나 문자열이 아님")
                continue
            if sid in seen:
                p.append(f"단계 id 중복: {sid}")
            seen.add(sid)
            for k in ("status", "duration_ms", "evidence", "output", "notes"):
                if k not in st:
                    p.append(f"{sid}: 필수 필드 없음: {k}")
            if st.get("status") not in STAGE_STATUSES:
                p.append(f"{sid}: status는 {list(STAGE_STATUSES)} 중 하나여야 함: {st.get('status')!r}")
            if st.get("duration_ms") is not None and not _is_num(st.get("duration_ms")):
                p.append(f"{sid}: duration_ms는 숫자 또는 null")
            ev = st.get("evidence")
            if not isinstance(ev, dict):
                p.append(f"{sid}: evidence가 객체가 아님")
                ev = {}
            else:
                if "*" not in ev:
                    p.append(f"{sid}: evidence에 기본값 '*'가 없음")
                for field, grade in ev.items():
                    if grade not in GRADES:
                        p.append(f"{sid}: 모르는 evidence 등급 {grade!r} ({field})")
            if not isinstance(st.get("output"), dict):
                p.append(f"{sid}: output이 객체가 아님")
            elif st.get("status") == "ok" and ev.get("*") != "unverified":
                for k in REQUIRED_OUTPUT.get(sid, ()):
                    if k not in st["output"]:
                        p.append(f"{sid}: output에 {k}가 없음")
                if sid == "tokenize" and isinstance(st["output"].get("tokens"), list):
                    for t in st["output"]["tokens"]:
                        if not isinstance(t, dict) or any(k not in t for k in TOKEN_KEYS):
                            p.append("tokenize: tokens 항목에 필수 키가 없음")
                            break
            if not isinstance(st.get("notes"), list) or not all(isinstance(n, str) for n in st.get("notes", [])):
                p.append(f"{sid}: notes는 문자열 목록이어야 함")
    return p


# ---------------------------------------------------------------- 기록 만들기 (S0 놀이터)
def _ms(ns):
    return None if ns is None else round(ns / 1e6, 3)


def build_playground_record(*, run_id, created_at, prompt, model, options, ollama_version, tokenizer, tok_result,
                            tok_ms, chunks, done, status, error=None):
    """놀이터 실행 하나의 기록. done은 스트림의 마지막(done) 사건이고, 오류·중단이면 None.

    오류·중단: 그때까지 만든 단계는 그대로 두고, 실패한 단계는 status "error"(notes에 이유),
    뒤의 단계는 "skipped". 전체 status는 error 또는 aborted.
    """
    msg = error or ("중단됨" if status == "aborted" else "알 수 없는 오류")
    settings = {k: options[k] for k in ("temperature", "num_predict", "num_ctx")}
    if "seed" in options:
        settings["seed"] = options["seed"]

    def stage(sid, st="ok", duration=None, evidence=None, output=None, notes=None):
        return {"id": sid, "status": st, "duration_ms": duration, "evidence": evidence or {"*": "measured"},
                "output": output if output is not None else {}, "notes": notes or []}

    def skipped(sid, why):
        return stage(sid, "skipped", None, {"*": "unverified"}, {}, [why])

    stages = [
        stage("input", evidence={"*": "input", "prompt_chars": "computed", "prompt_bytes": "computed"},
              output={"prompt": prompt, "prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8"))}),
        stage("tokenize", duration=tok_ms,
              evidence={"*": "measured", "bytes_match": "computed", "token_count": "computed"},
              output={k: tok_result[k] for k in ("text_chars", "text_bytes", "nfc_changed", "token_count",
                                                  "partial_count", "bytes_match")}
              | {"tokens": [{k: t[k] for k in TOKEN_KEYS} for t in tok_result["tokens"]]}),
    ]
    local_tokens = tok_result["token_count"]
    chunk_rows = [{k: c[k] for k in ("n", "text", "gap_ms", "since_start_ms")} for c in chunks]
    first_chunk_ms = chunk_rows[0]["since_start_ms"] if chunk_rows else None

    if done is not None:
        s = done["stats"]
        eval_count, eval_ns = s.get("eval_count"), s.get("eval_duration")
        tok_s = round(eval_count / (eval_ns / 1e9), 3) if eval_count is not None and eval_ns else None
        pe = s.get("prompt_eval_count")
        prefill_out = {"prompt_eval_count": pe, "prompt_eval_ms": _ms(s.get("prompt_eval_duration")),
                       "local_token_count": local_tokens, "count_diff": None if pe is None else pe - local_tokens}
        if "prompt_eval_cached_count" in s:
            prefill_out["prompt_eval_cached_count"] = s["prompt_eval_cached_count"]
        joined = done["joined"]
        final = done.get("final_response")
        stages += [
            stage("load", duration=_ms(s.get("load_duration")), output={"load_ms": _ms(s.get("load_duration"))}),
            stage("prefill", duration=_ms(s.get("prompt_eval_duration")), output=prefill_out,
                  evidence={"*": "measured", "local_token_count": "computed", "count_diff": "computed"}),
            stage("decode", duration=_ms(eval_ns),
                  evidence={"*": "measured", "tok_s": "computed"},
                  output={"chunks": chunk_rows, "chunk_count": len(chunk_rows), "eval_count": eval_count,
                          "eval_ms": _ms(eval_ns),
                          "tok_s": tok_s, "done_reason": s.get("done_reason"), "first_chunk_ms": first_chunk_ms,
                          "total_ms": _ms(s.get("total_duration"))}),
            stage("restore",
                  evidence={"*": "measured", "chars": "computed", "bytes": "computed",
                            "final_response_empty": "computed"},
                  output={"server_joined": joined, "final_response": final, "final_response_empty": not final,
                          "chars": len(joined), "bytes": len(joined.encode("utf-8"))}),
        ]
        run_status, run_error = "ok", None
    else:
        note = f"{'중단' if status == 'aborted' else '오류'}: {msg}"
        if not chunk_rows:
            # 어느 단계에서 멈췄는지 알 수 없다(통계는 마지막 줄에 온다). 첫 Ollama 단계를 실패 지점으로 둔다.
            stages += [
                stage("load", "error", None, {"*": "unverified"}, {},
                      [note, "첫 조각이 오기 전에 연결이 끊겨 올리기와 읽기 중 어느 단계에서 멈췄는지 알 수 없다. "
                             "올리기 단계에 기록한다."]),
                skipped("prefill", "앞 단계가 끝나지 않아 건너뜀"),
                skipped("decode", "앞 단계가 끝나지 않아 건너뜀"),
            ]
        else:
            why = ("첫 조각이 도착했으므로 이 단계는 끝났지만, "
                   "시간과 개수는 마지막 통계 줄에 오는데 그 줄을 받지 못해 값이 없음")
            stages += [
                stage("load", "ok", None, {"*": "unverified"}, {}, [why]),
                stage("prefill", "ok", None, {"*": "unverified"}, {}, [why]),
                stage("decode", "error", None, {"*": "measured", "chunk_count": "computed"},
                      {"chunks": chunk_rows, "chunk_count": len(chunk_rows), "first_chunk_ms": first_chunk_ms},
                      [note, f"받은 조각 {len(chunk_rows)}개까지만 기록함"]),
            ]
        stages.append(skipped("restore", "decode가 끝나지 않아 건너뜀"))
        run_status, run_error = ("aborted" if status == "aborted" else "error"), msg

    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": created_at, "kind": "playground",
        "title": prompt[:40], "exportable": False,
        "redaction": {"applied": False, "note": "사용자가 직접 입력한 글. 마스킹 단계 없음(S0)"},
        "environment": {"ollama_version": ollama_version, "model": model, "tokenizer": dict(tokenizer)},
        "settings": settings, "status": run_status, "error": run_error, "stages": stages,
    }


def index_entry(record: dict) -> dict:
    return {"run_id": record["run_id"], "created_at": record["created_at"], "kind": record["kind"],
            "title": record["title"],
            "model": record["environment"]["model"], "status": record["status"], "exportable": record["exportable"]}


# ---------------------------------------------------------------- 파일 쓰기·읽기
def _atomic_write(path: Path, data: bytes) -> None:
    """같은 폴더에 임시 파일을 쓰고 os.replace로 바꿔 넣는다. 실패하면 임시 파일을 지운다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_index(data_dir) -> list:
    try:
        v = json.loads((Path(data_dir) / "index.json").read_text(encoding="utf-8"))
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def write_run(data_dir, record: dict) -> bool:
    """검증 -> 크기 확인 -> 기록 파일 -> index.json 갱신. 어떤 실패도 예외로 내보내지 않고 표준 오류에만 남긴다."""
    try:
        problems = validate_bundle(record)
        if problems:
            print(f"실행 기록을 쓰지 않음(검증 실패): {problems}", file=sys.stderr)
            return False
        data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(data) > MAX_RECORD_BYTES:
            print(f"실행 기록을 쓰지 않음(크기 {len(data)}B > {MAX_RECORD_BYTES}B): {record.get('run_id')}",
                  file=sys.stderr)
            return False
        data_dir = Path(data_dir)
        target = data_dir / "runs" / f"{record['run_id']}.json"
        if target.exists():
            print(f"실행 기록을 쓰지 않음(같은 이름의 파일이 있음): {target.name}", file=sys.stderr)
            return False
        _atomic_write(target, data)                        # 기록 파일을 먼저: index가 없는 파일을 가리키지 않게
        with _index_lock:
            entries = [e for e in read_index(data_dir) if isinstance(e, dict) and e.get("run_id") != record["run_id"]]
            entries.insert(0, index_entry(record))         # 최신순
            entries = entries[:INDEX_MAX]
            index_bytes = (json.dumps(entries, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
            _atomic_write(data_dir / "index.json", index_bytes)
        return True
    except Exception as e:  # noqa: BLE001  (기록 실패가 응답 스트림을 망치면 안 된다)
        print(f"실행 기록 쓰기 실패: {e!r}", file=sys.stderr)
        return False
