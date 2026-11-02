"""파이프라인 탐험 사이트: 실행 기록(JSON 파일)을 읽어 보여 주는 뷰어의 서버 쪽.

- 화면은 파일만 읽는다: ./data/index.json, ./data/runs/<run_id>.json, ./content/*.json
- 실행(run)과 토큰화처럼 '만드는' 기능만 ./api/... 를 쓴다. 실행 하나가 끝나면 기록 파일 하나가 남는다(runs.py).
"""
import http.client
import json
import os
import threading
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from tokenizers import Tokenizer

import limits
import ollama
import runs
import streamparse
import tokenization
import tokenizer_pin

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.97.0.1:11434")   # 서버 설정값. 사용자 입력으로 바꿀 수 없다
TOKENIZER_PATH = os.environ.get("TOKENIZER_PATH", "/opt/tokenizer/tokenizer.json")
# 볼륨 explorer-traces. runs/와 index.json이 여기에 쌓인다
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_EXT = {".js": "text/javascript", ".css": "text/css", ".json": "application/json"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024          # 8000자 x 이모지 이스케이프(12바이트)도 들어가는 크기

_busy = threading.Lock()                                # Ollama를 부르는 요청(run, probe)은 한 번에 하나
PROBE_PROMPT = "안녕"
_version_cache = {"t": 0.0, "v": None}


@lru_cache(maxsize=1)
def get_tokenizer() -> Tokenizer:
    return Tokenizer.from_file(TOKENIZER_PATH)


def _ollama_version():
    """기록의 environment에 넣을 Ollama 버전. 실행마다 부르지 않게 5분 캐시한다."""
    if time.monotonic() - _version_cache["t"] > 300:
        _version_cache.update(t=time.monotonic(), v=ollama.version(OLLAMA_URL))
    return _version_cache["v"]


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # 인라인 스크립트가 없으므로 'unsafe-inline'을 쓰지 않는다.
    # 외부 주소(CDN·글꼴·외부 API)로 나가는 요청은 브라우저가 막는다.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'")
    return resp


def _err(status: int, message: str):
    return jsonify({"error": message}), status


@app.errorhandler(413)
def _too_large(_e):
    return _err(413, "요청 본문이 너무 큽니다.")


def _json_body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise limits.InvalidInput("JSON 객체 본문이 필요합니다.")
    return payload


class _Slot:
    """_busy 자물쇠를 정확히 한 번만 풀기 위한 껍데기. 응답이 끝나거나 끊길 때 풀린다."""

    def __init__(self):
        self._released = False

    def release(self):
        if not self._released:
            self._released = True
            _busy.release()


# ---------------------------------------------------------------- 화면 파일과 기록 읽기 (파일만 읽는 경로)
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<any(js,css,content):kind>/<path:name>")
def static_files(kind: str, name: str):
    """static/{js,css,content} 아래의 .js .css .json 만. 경로 이탈은 send_from_directory가 막는다."""
    ext = os.path.splitext(name)[1]
    if ext not in STATIC_EXT:
        abort(404)
    return send_from_directory(STATIC_DIR / kind, name, mimetype=STATIC_EXT[ext])


@app.get("/data/index.json")
def data_index():
    path = Path(DATA_DIR) / "index.json"
    if not path.is_file():                              # 아직 실행이 없으면 빈 목록
        return Response("[]\n", mimetype="application/json")
    return send_from_directory(DATA_DIR, "index.json", mimetype="application/json")


@app.get("/data/runs/<run_id>.json")
def data_run(run_id: str):
    if not runs.RUN_ID_RE.fullmatch(run_id):            # 정규식에 맞지 않으면 파일 시스템을 보지도 않는다
        abort(404)
    return send_from_directory(Path(DATA_DIR) / "runs", f"{run_id}.json", mimetype="application/json")


# /data 아래의 다른 파일(예: 옛 traces.jsonl)은 라우트가 없으므로 404다.


# ---------------------------------------------------------------- 만드는 기능 (live에서만 쓰는 ./api/...)
@app.get("/api/config")
def api_config():
    try:
        models, ollama_error = ollama.list_models(OLLAMA_URL), None
    except ollama.OllamaError as e:
        models, ollama_error = [], str(e)
    return jsonify({
        "models": models,
        "ollama_error": ollama_error,
        "limits": {"prompt_max": limits.PROMPT_MAX,
                   "num_predict": [limits.NUM_PREDICT_MIN, limits.NUM_PREDICT_MAX],
                   "num_ctx": list(limits.NUM_CTX_CHOICES),
                   "temperature": [limits.TEMPERATURE_MIN, limits.TEMPERATURE_MAX],
                   "seed": [limits.SEED_MIN, limits.SEED_MAX]},
        "defaults": limits.DEFAULTS,
        "tokenizer": {"repo": tokenizer_pin.REPO, "revision": tokenizer_pin.REVISION, "sha256": tokenizer_pin.SHA256},
    })


@app.post("/api/tokenize")
def api_tokenize():
    try:
        text = limits.validate_text(_json_body().get("text"))
    except limits.InvalidInput as e:
        return _err(400, str(e))
    return jsonify(tokenization.tokenize_text(get_tokenizer(), text))


@app.get("/api/show")
def api_show():
    """모델이 질문 앞뒤에 붙이는 틀(template). 확인용으로 그대로 가져온다."""
    try:
        model = request.args.get("model", "")
        if model not in ollama.list_models(OLLAMA_URL):
            return _err(400, "허용되지 않은 model입니다.")
        info = ollama.show_model(OLLAMA_URL, model)
    except ollama.OllamaError as e:
        return _err(502, str(e))
    return jsonify({"model": model, "template": info.get("template"), "details": info.get("details")})


@app.get("/api/probe")
def api_probe():
    """Ollama가 토큰별 확률(logprobs)을 내주는지 작은 요청으로 확인한다. 응답에 있는 것만 그대로 돌려준다."""
    if not _busy.acquire(blocking=False):
        return _err(409, "이미 처리 중인 요청이 있습니다.")
    try:
        model = request.args.get("model", "")
        if model not in ollama.list_models(OLLAMA_URL):
            return _err(400, "허용되지 않은 model입니다.")
        body = {"model": model, "prompt": PROBE_PROMPT, "stream": False, "logprobs": True, "top_logprobs": 5,
                "options": {"num_predict": 3}}
        status, raw = ollama.request_json(OLLAMA_URL, "POST", "/api/generate", body, read_timeout=ollama.READ_TIMEOUT)
        result = {"ollama_version": ollama.version(OLLAMA_URL), "http_status": status, "request_extra": {
            "logprobs": True, "top_logprobs": 5, "num_predict": 3}}
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if status != 200:
            result.update(verdict="error", error=raw[:500].decode("utf-8", "replace"))
        elif not isinstance(parsed, dict) or not parsed.get("logprobs"):
            result.update(verdict="no_field", response_keys=sorted(parsed) if isinstance(parsed, dict) else None)
        else:
            entries = parsed["logprobs"]
            first = entries[0] if isinstance(entries, list) and entries else None
            result.update(verdict="supported", n_entries=len(entries), response_keys=sorted(parsed),
                          entry_keys=sorted(first) if isinstance(first, dict) else None,
                          has_top_logprobs=bool(isinstance(first, dict) and first.get("top_logprobs")),
                          first_entry=first)
        return jsonify(result)
    except ollama.OllamaError as e:
        return _err(502, str(e))
    finally:
        _busy.release()


@app.post("/api/run")
def api_run():
    if not _busy.acquire(blocking=False):
        return _err(409, "이미 처리 중인 요청이 있습니다. 끝난 뒤 다시 시도하세요.")
    slot, handed_off = _Slot(), False
    try:
        req = limits.validate_run_request(_json_body(), ollama.list_models(OLLAMA_URL))
        resp = Response(_run_stream(req), mimetype="application/x-ndjson",
                        headers={"X-Accel-Buffering": "no"})
        resp.call_on_close(slot.release)               # 스트림이 끝나거나 클라이언트가 끊으면 자물쇠를 푼다
        handed_off = True
        return resp
    except limits.InvalidInput as e:
        return _err(400, str(e))
    except ollama.OllamaError as e:
        return _err(502, str(e))
    finally:
        if not handed_off:
            slot.release()


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _run_stream(req: dict):
    """NDJSON 사건을 하나씩 내보낸다: start -> chunk* -> done(또는 error). 모든 실행은 기록 파일 하나를 남긴다.

    start와 done(또는 error) 사건에 run_id를 담는다. 기록은 done/error 사건을 내보내기 전에 쓴다
    (화면이 run_id를 받고 바로 기록을 읽어도 이미 있게). 끊기면(aborted) finally에서 쓴다.
    사용자가 요청한 계산(토큰화)은 서버가 여기서 한 번만 하고 기록에 넣는다. 브라우저가 계산한 값은 저장하지 않는다.
    """
    options = req["options"]
    now = datetime.now(UTC)
    runs_dir = Path(DATA_DIR) / "runs"
    run_id = runs.make_run_id(now, exists=lambda rid: (runs_dir / f"{rid}.json").exists())
    created_at = runs.utc_iso(now)

    try:
        t = time.perf_counter()
        tok_result = tokenization.tokenize_text(get_tokenizer(), req["prompt"])
        tok_ms = round((time.perf_counter() - t) * 1000, 3)
    except Exception as e:  # noqa: BLE001
        yield _line({"type": "error", "message": f"토큰화 실패: {e}"})
        return
    version = _ollama_version()

    chunks, done, status, error = [], None, "ok", None
    saved, conn = False, None

    def save():
        nonlocal saved
        if saved:
            return
        saved = True
        record = runs.build_playground_record(
            run_id=run_id, created_at=created_at, prompt=req["prompt"], model=req["model"], options=options,
            ollama_version=version, tokenizer={"repo": tokenizer_pin.REPO, "revision": tokenizer_pin.REVISION,
                                               "sha256": tokenizer_pin.SHA256},
            tok_result=tok_result, tok_ms=tok_ms, chunks=chunks, done=done, status=status, error=error)
        runs.write_run(DATA_DIR, record)

    try:
        yield _line({"type": "start", "run_id": run_id, "model": req["model"], "options": options,
                     "local_tokens": tok_result["token_count"]})
        t0 = time.monotonic()                          # '시작' = 요청을 보내기 시작한 시각(연결 포함)
        conn, resp = ollama.open_generate_stream(
            OLLAMA_URL, {"model": req["model"], "prompt": req["prompt"], "options": options})
        if resp.status != 200:
            raise ollama.OllamaError(f"HTTP {resp.status} {resp.read()[:300].decode('utf-8', 'replace')}")
        for ev in streamparse.parse_stream(iter(resp.readline, b""), time.monotonic, t0):
            if ev["type"] == "chunk":
                chunks.append(ev)
            elif ev["type"] == "done":
                done = ev
                save()
                ev = {**ev, "run_id": run_id}
            elif ev["type"] == "error":
                status, error = "error", ev["message"]
                save()
                ev = {**ev, "run_id": run_id}
            yield _line(ev)
    except GeneratorExit:                              # 브라우저가 끊음. Ollama 연결도 닫아 생성을 멈추게 한다.
        status = "aborted"
        raise
    except (ollama.OllamaError, OSError, http.client.HTTPException) as e:
        status, error = "error", str(e)
        save()
        yield _line({"type": "error", "message": str(e), "run_id": run_id})
    finally:
        if conn is not None:
            conn.close()
        save()                                         # 이미 썼으면 아무 일도 하지 않는다
