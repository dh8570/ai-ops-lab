"""explorer/를 import 경로에 넣고, 진짜 tokenizer.json과 가짜 Ollama 서버를 제공한다. 외부 네트워크는 쓰지 않는다."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session")
def tk():
    """진짜 Qwen2.5 tokenizer.json. check-explorer.sh가 받아 TOKENIZER_PATH로 알려 준다.
    없으면 건너뛰지 않고 실패시킨다."""
    from tokenizers import Tokenizer
    path = os.environ.get("TOKENIZER_PATH", "/opt/tokenizer/tokenizer.json")
    if not Path(path).is_file():
        pytest.fail(f"tokenizer.json이 없습니다: {path} (scripts/check-explorer.sh로 실행하세요)")
    return Tokenizer.from_file(path)


class FakeOllama:
    """/api/tags, /api/show, /api/generate만 흉내 내는 로컬 서버(스레드)."""

    def __init__(self):
        self.models = ["fake:1b"]
        self.chunks = ["안", "녕", "하세요"]
        self.logprobs = "supported"          # supported | no_field | error
        self.generate_requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, json.dumps({"models": [{"name": m} for m in outer.models]}).encode())
                elif self.path == "/api/version":
                    self._send(200, b'{"version":"0.0.test"}')
                else:
                    self._send(404, b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/show":
                    info = {"template": "<tpl>{{ .Prompt }}</tpl>", "details": {"family": "fake"}}
                    self._send(200, json.dumps(info).encode())
                elif self.path == "/api/generate":
                    outer.generate_requests.append(body)
                    if not body.get("stream", True):
                        if outer.logprobs == "error":
                            return self._send(400, b'{"error":"unknown field logprobs"}')
                        resp = {"response": "abc", "done": True}
                        if outer.logprobs == "supported":
                            resp["logprobs"] = [{"token": "a", "logprob": -0.1, "bytes": [97],
                                                 "top_logprobs": [{"token": "a", "logprob": -0.1, "bytes": [97]}]}]
                        return self._send(200, json.dumps(resp).encode())
                    lines = [{"model": body["model"], "response": c, "done": False} for c in outer.chunks]
                    lines.append({"model": body["model"], "response": "", "done": True, "done_reason": "stop",
                                  "context": [1, 2, 3], "total_duration": 3_000_000_000, "load_duration": 1_000_000_000,
                                  "prompt_eval_count": 31, "prompt_eval_cached_count": 0,
                                  "prompt_eval_duration": 500_000_000, "eval_count": len(outer.chunks),
                                  "eval_duration": 1_500_000_000})
                    self._send(200, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines).encode(),
                               "application/x-ndjson")
                else:
                    self._send(404, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_ollama():
    f = FakeOllama()
    yield f
    f.close()
