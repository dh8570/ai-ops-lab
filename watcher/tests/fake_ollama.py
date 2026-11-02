"""시험용 가짜 Ollama(로컬 임시 포트). 실제 Ollama 없이 llm_client를 시험한다.

모드: ok, slow(delay초 지연), slowdrip(바이트를 조금씩 오래 보냄), drop(응답 없이 연결을 끊음), http500,
notfound(404 model not found), garbage(JSON 아닌 본문), nocontent(message.content 없음), truncated(done_reason length),
version(/api/version이 정상). /api/version은 failure 모드에서는 같은 방식으로 실패하고, 나머지 모드에서는 정상 응답한다.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("ok", "slow", "slowdrip", "drop", "http500", "notfound", "garbage", "nocontent", "truncated", "version")

OK_BODY = {
    "model": "qwen2.5:7b",
    "created_at": "2026-11-02T00:00:00Z",
    "message": {"role": "assistant", "content": '{"summary":"가짜 응답","causes":["a","b"]}'},
    "done": True,
    "done_reason": "stop",
    "total_duration": 2_500_000_000,
    "load_duration": 1_000_000_000,
    "prompt_eval_count": 50,
    "prompt_eval_cached_count": 32,
    "prompt_eval_duration": 300_000_000,
    "eval_count": 40,
    "eval_duration": 1_000_000_000,
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.fake.requests.append({"method": "GET", "path": self.path, "body": b""})
        self._serve("version")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.fake.requests.append({"method": "POST", "path": self.path, "body": body})
        self._serve("chat")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drip(self, body: bytes) -> None:
        """헤더는 바로 보내고 본문은 한 바이트씩 interval초 간격으로 보낸다(서버가 멈추라고 하면 중단)."""
        fake = self.server.fake
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.flush()
        for i in range(len(body)):
            self.wfile.write(body[i:i + 1])
            self.wfile.flush()
            if fake.stop.wait(fake.interval):
                return

    def _serve(self, kind: str) -> None:
        fake = self.server.fake
        mode = fake.mode
        try:
            if mode == "drop":
                self.close_connection = True
                return
            if mode == "slow" and fake.stop.wait(fake.delay):
                return
            if mode == "http500":
                return self._send(500, b"boom")
            if mode == "notfound":
                return self._send(404, json.dumps({"error": "model 'x' not found"}).encode())
            if mode == "garbage":
                return self._send(200, b"<html>not json</html>")
            if kind == "version":
                payload = {"version": "0.40.2"}
            else:
                payload = dict(OK_BODY)
                if mode == "nocontent":
                    payload.pop("message")
                if mode == "truncated":
                    payload["done_reason"] = "length"
            body = json.dumps(payload).encode()
            if mode == "slowdrip":
                return self._drip(body)
            self._send(200, body)
        except OSError:
            pass                                    # 클라이언트가 먼저 끊은 경우


class FakeOllama:
    """with FakeOllama("ok") as fake: fake.url, fake.requests, fake.mode(바꿀 수 있음)"""

    def __init__(self, mode: str = "ok", delay: float = 3.0, interval: float = 0.2):
        assert mode in MODES, mode
        self.mode, self.delay, self.interval = mode, delay, interval
        self.requests: list[dict] = []
        self.stop = threading.Event()               # 지연·드립 중인 핸들러를 풀어 준다
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self._server.shutdown()
        self._server.server_close()
        return False
