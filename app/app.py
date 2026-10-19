import json
import os
import time
import uuid
from datetime import datetime, timezone

import psycopg
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

SERVICE = "visit-counter"
DB_URL = os.environ["DATABASE_URL"]
LOG_PATH = os.environ.get("LOG_PATH", "/var/log/app/app.jsonl")

app = Flask(__name__)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log_file = open(LOG_PATH, "a", buffering=1, encoding="utf-8")  # 줄 단위로 즉시 기록


def write_log(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    log_file.write(line + "\n")
    print(line, flush=True)  # docker compose logs에서도 보이게


def db():
    return psycopg.connect(DB_URL, connect_timeout=3)


with db() as conn:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS visits ("
        "id BIGSERIAL PRIMARY KEY, visited_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


@app.before_request
def start_request():
    g.start = time.perf_counter()
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    g.error = None


@app.after_request
def access_log(response):
    write_log({
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "service": SERVICE,
        "request_id": g.request_id,
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "latency_ms": round((time.perf_counter() - g.start) * 1000, 1),
        "error": g.error,
    })
    response.headers["X-Request-ID"] = g.request_id
    return response


@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):  # 404 같은 정상적인 HTTP 에러는 그대로
        return e
    g.error = f"{type(e).__name__}: {e}"
    return jsonify(error="internal error", request_id=g.request_id), 500


@app.get("/")
def index():
    with db() as conn:
        conn.execute("INSERT INTO visits DEFAULT VALUES")
        count = conn.execute("SELECT count(*) FROM visits").fetchone()[0]
    return jsonify(service=SERVICE, visits=count)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/error")
def error():
    raise RuntimeError("simulated failure (/error)")


@app.get("/slow")
def slow():
    delay = min(float(request.args.get("delay", 3)), 10)
    time.sleep(delay)
    return jsonify(delayed_s=delay)