"""파이프라인 탐험 사이트 S0: 문장 -> 토큰화 -> 읽기·쓰기(스트리밍) -> 글자 복원. 화면의 숫자는 모두 실측이다."""
import http.client
import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from tokenizers import Tokenizer

import limits
import ollama
import streamparse
import tokenization
import tokenizer_pin
import traces

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.97.0.1:11434")   # 서버 설정값. 사용자 입력으로 바꿀 수 없다
TOKENIZER_PATH = os.environ.get("TOKENIZER_PATH", "/opt/tokenizer/tokenizer.json")
TRACE_PATH = os.environ.get("TRACE_PATH", "/data/traces.jsonl")
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024          # 8000자 x 이모지 이스케이프(12바이트)도 들어가는 크기

_busy = threading.Lock()                                # Ollama를 부르는 요청(run, probe)은 한 번에 하나
PROBE_PROMPT = "안녕"


@lru_cache(maxsize=1)
def get_tokenizer() -> Tokenizer:
    return Tokenizer.from_file(TOKENIZER_PATH)


@app.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # 외부 주소로 나가는 요청(CDN·글꼴·외부 API)을 브라우저가 막게 한다. 같은 출처(connect-src 'self')만 허용.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
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


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


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
    """NDJSON 사건을 하나씩 내보낸다: start -> chunk* -> done(또는 error). 끝나면 traces.jsonl에 한 줄 남긴다."""
    options = req["options"]
    local_tokens = tokenization.count_tokens(get_tokenizer(), req["prompt"])
    t0 = time.monotonic()                              # '시작' = 요청을 보내기 시작한 시각(연결 포함)
    status, error, parts, final = "ok", None, [], {}
    conn = None
    try:
        yield _line({"type": "start", "model": req["model"], "options": options, "local_tokens": local_tokens})
        conn, resp = ollama.open_generate_stream(
            OLLAMA_URL, {"model": req["model"], "prompt": req["prompt"], "options": options})
        if resp.status != 200:
            raise ollama.OllamaError(f"HTTP {resp.status} {resp.read()[:300].decode('utf-8', 'replace')}")
        for ev in streamparse.parse_stream(iter(resp.readline, b""), time.monotonic, t0):
            if ev["type"] == "chunk":
                parts.append(ev["text"])
            elif ev["type"] == "done":
                final = ev
            elif ev["type"] == "error":
                status, error = "error", ev["message"]
            yield _line(ev)
    except GeneratorExit:                              # 브라우저가 끊음. Ollama 연결도 닫아 생성을 멈추게 한다.
        status = "aborted"
        raise
    except (ollama.OllamaError, OSError, http.client.HTTPException) as e:
        status, error = "error", str(e)
        yield _line({"type": "error", "message": str(e)})
    finally:
        if conn is not None:
            conn.close()
        stats = final.get("stats", {})
        traces.append_trace(TRACE_PATH, {
            "status": status, "error": error, "model": req["model"], "options": options, "prompt": req["prompt"],
            "local_tokens": local_tokens, "chunks": len(parts), "answer": "".join(parts), "stats": stats,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1)})
