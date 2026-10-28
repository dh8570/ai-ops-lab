"""Ollama 호출(표준 라이브러리만). 주소는 서버 설정(OLLAMA_URL)에서만 온다. 사용자 입력은 주소에 들어가지 않는다."""
import http.client
import json
import urllib.parse

CONNECT_TIMEOUT = 5      # 연결 제한(초)
READ_TIMEOUT = 180       # 응답 대기(초). 모델이 내려가 있으면 첫 응답이 늦을 수 있다.
SHORT_READ_TIMEOUT = 10  # 목록·정보 조회


class OllamaError(Exception):
    """Ollama에 닿지 못했거나 이해할 수 없는 응답."""


def _connect(base_url: str, read_timeout: float) -> http.client.HTTPConnection:
    u = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
    except OSError as e:
        conn.close()
        raise OllamaError(f"Ollama에 연결하지 못했습니다: {e}") from e
    conn.sock.settimeout(read_timeout)               # 연결 뒤에는 응답 대기 시간으로 바꾼다
    return conn


def request_json(base_url: str, method: str, path: str, body=None, read_timeout: float = SHORT_READ_TIMEOUT):
    """(HTTP 상태, 응답 바이트). 네트워크 오류는 OllamaError."""
    conn = _connect(base_url, read_timeout)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise OllamaError(f"Ollama 요청 실패({path}): {e}") from e
    finally:
        conn.close()


def _json_or_error(status: int, raw: bytes, what: str) -> dict:
    if status != 200:
        raise OllamaError(f"{what}: HTTP {status} {raw[:200].decode('utf-8', 'replace')}")
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise OllamaError(f"{what}: JSON이 아닌 응답") from e
    if not isinstance(obj, dict):
        raise OllamaError(f"{what}: 객체가 아닌 응답")
    return obj


def list_models(base_url: str) -> list[str]:
    obj = _json_or_error(*request_json(base_url, "GET", "/api/tags"), "/api/tags")
    return [m["name"] for m in obj.get("models", []) if isinstance(m, dict) and "name" in m]


def show_model(base_url: str, model: str) -> dict:
    return _json_or_error(*request_json(base_url, "POST", "/api/show", {"model": model}), "/api/show")


def version(base_url: str) -> str | None:
    try:
        return _json_or_error(*request_json(base_url, "GET", "/api/version"), "/api/version").get("version")
    except OllamaError:
        return None


def open_generate_stream(base_url: str, body: dict):
    """/api/generate를 stream:true로 열고 (연결, 응답)을 돌려준다. 호출한 쪽이 연결을 닫는다."""
    conn = _connect(base_url, READ_TIMEOUT)
    try:
        payload = json.dumps({**body, "stream": True}).encode("utf-8")
        conn.request("POST", "/api/generate", body=payload, headers={"Content-Type": "application/json"})
        return conn, conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise OllamaError(f"/api/generate 요청 실패: {e}") from e
