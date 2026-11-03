#!/usr/bin/env python3
"""LLM 알림 합격 시험(F·G·H)용 가짜 Ollama 서버. 표준 라이브러리만, 다른 파일을 import하지 않는다.

실행 위치: compose 프로필 llmtest의 fake-ollama 컨테이너 (docs/acceptance/llm-scenarios.md)
    python fake_ollama_server.py [--host 0.0.0.0] [--port 11434]
호스트로 공개하지 않는다. compose 네트워크 안에서 watcher가 http://fake-ollama:11434 로 부른다.

Ollama처럼 답하는 주소
    POST /api/chat     분석 요청. 모드에 따라 응답한다.
    GET  /api/version  {"version": "fake"}

시험이 쓰는 제어 주소 (비밀값 없음, 같은 네트워크의 누구나 부를 수 있다)
    POST /__mode       {"mode": "...", "delay": 초}  실행 중에 모드를 바꾼다. delay는 생략하면 그대로
    GET  /__mode       {"mode", "delay", "chat_requests"}  현재 모드와 지금까지 받은 /api/chat 수
    GET  /__last       마지막 /api/chat 요청 본문(프롬프트 확인용). 아직 없으면 404

모드
    analysis  유효한 분석 JSON(summary 1, causes 2, checks 2)을 바로 돌려준다
    slow      delay초 기다린 뒤 analysis와 같은 응답 (watcher의 30초 상한을 넘기는 시험용)
    drop      응답 없이 연결을 끊는다
    http500   HTTP 500
    garbage   JSON이 아닌 본문
"""
import argparse
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("analysis", "slow", "drop", "http500", "garbage")
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_DELAY_SEC = 600

# watcher/analysis.py의 ANALYSIS_SCHEMA를 만족한다(summary 1개, causes 2개, checks 2개). 한국어만 쓴다.
ANALYSIS_ANSWER = {
    "summary": "최근 창에서 5xx 응답이 급증해 서비스 오류율이 평소보다 크게 높아졌다.",
    "causes": ["특정 경로에서 예외가 반복되어 500 응답이 늘었을 수 있다.",
               "앱이 의존하는 데이터베이스 연결에 문제가 생겼을 수 있다."],
    "checks": ["경로별 5xx 건수와 오류 유형을 확인한다.",
               "데이터베이스 상태와 앱 로그의 최근 오류를 확인한다."],
}

# Ollama가 주는 통계 필드(시간은 나노초)
STATS = {
    "total_duration": 2_500_000_000,
    "load_duration": 1_000_000_000,
    "prompt_eval_count": 400,
    "prompt_eval_cached_count": 320,
    "prompt_eval_duration": 300_000_000,
    "eval_count": 60,
    "eval_duration": 1_000_000_000,
}


class State:
    """모드와 받은 요청. 요청마다 다른 스레드가 건드리므로 잠금으로 지킨다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "analysis"
        self.delay = 0.0
        self.chat_requests = 0
        self.last_body: bytes | None = None
        self.stop = threading.Event()        # 종료할 때 지연 중인 요청을 풀어 준다

    def snapshot(self) -> dict:
        with self.lock:
            return {"mode": self.mode, "delay": self.delay, "chat_requests": self.chat_requests}


def analysis_body(model: str) -> bytes:
    payload = {
        "model": model,
        "created_at": "2026-11-02T00:00:00Z",
        "message": {"role": "assistant", "content": json.dumps(ANALYSIS_ANSWER, ensure_ascii=False)},
        "done": True,
        "done_reason": "stop",
        **STATS,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-ollama"

    def log_message(self, *args):
        pass

    # ---- 공통
    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self) -> bytes | None:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_REQUEST_BYTES:
            self._json(400, {"error": "bad content-length"})
            return None
        return self.rfile.read(n)

    # ---- GET
    def do_GET(self):
        st: State = self.server.state
        try:
            if self.path == "/api/version":
                self._json(200, {"version": "fake"})
            elif self.path == "/__mode":
                self._json(200, st.snapshot())
            elif self.path == "/__last":
                with st.lock:
                    last = st.last_body
                if last is None:
                    self._json(404, {"error": "no /api/chat request yet"})
                else:
                    self._send(200, last)
            else:
                self._json(404, {"error": "not found"})
        except OSError:
            pass                                    # 클라이언트가 먼저 끊은 경우

    # ---- POST
    def do_POST(self):
        try:
            body = self._read_body()
            if body is None:
                return
            if self.path == "/__mode":
                self._set_mode(body)
            elif self.path == "/api/chat":
                self._chat(body)
            else:
                self._json(404, {"error": "not found"})
        except OSError:
            pass

    def _set_mode(self, body: bytes) -> None:
        st: State = self.server.state
        try:
            req = json.loads(body)
            mode = req["mode"]
            delay = req.get("delay")
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": 'body는 {"mode": ..., "delay": 초} 형식이어야 한다'})
        if mode not in MODES:
            return self._json(400, {"error": f"mode는 {list(MODES)} 중 하나여야 한다"})
        if delay is not None and (isinstance(delay, bool) or not isinstance(delay, int | float)
                                  or not 0 <= delay <= MAX_DELAY_SEC):
            return self._json(400, {"error": f"delay는 0~{MAX_DELAY_SEC} 사이 숫자여야 한다"})
        with st.lock:
            st.mode = mode
            if delay is not None:
                st.delay = float(delay)
        self._json(200, st.snapshot())

    def _chat(self, body: bytes) -> None:
        st: State = self.server.state
        with st.lock:
            st.chat_requests += 1
            st.last_body = body                     # 지연·끊기 전에 기록한다(시험이 요청 내용을 볼 수 있게)
            mode, delay = st.mode, st.delay
        try:
            model = json.loads(body).get("model", "fake")
        except (ValueError, AttributeError):
            model = "fake"
        if not isinstance(model, str):
            model = "fake"

        if mode == "drop":
            self.close_connection = True            # 응답을 쓰지 않고 연결을 닫는다
            return
        if mode == "http500":
            return self._send(500, b"boom", "text/plain")
        if mode == "garbage":
            return self._send(200, b"<html>not json</html>", "text/html")
        if mode == "slow" and st.stop.wait(delay):
            return                                  # 서버가 끝나는 중이면 응답하지 않는다
        self._send(200, analysis_body(model))


def make_server(host: str = "0.0.0.0", port: int = 11434) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.state = State()
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description="합격 시험용 가짜 Ollama")
    ap.add_argument("--host", default="0.0.0.0", help="컨테이너 안에서 듣는 주소(호스트로는 공개하지 않는다)")
    ap.add_argument("--port", type=int, default=11434)
    args = ap.parse_args()
    server = make_server(args.host, args.port)
    # docker stop(SIGTERM)에 바로 끝나도록 한다
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    print(f"fake-ollama 시작 {args.host}:{args.port} 모드 analysis", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.state.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
