"""Ollama /api/chat 호출(표준 라이브러리만).

감시 루프가 부르게 될 코드라서 어떤 실패(연결 거부, 시간 초과, 깨진 응답, 예기치 못한 예외)도 예외로 내보내지 않고
실패 이유 코드가 담긴 LlmResult로 돌려준다. 주소는 서버 설정(OLLAMA_URL)에서만 오며 사용자 입력은 들어가지 않는다.
"""
import http.client
import json
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, fields

MAX_BODY_BYTES = 2 * 1024 * 1024        # 응답 본문 상한. 이보다 크면 bad_response
_BASE_URL_RE = re.compile(r"http://[A-Za-z0-9.-]+:(\d{1,5})")
_NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class LlmOptions:
    """샘플링 옵션 7개. 기본값이 없어서 하나라도 빠뜨리면 만들 때부터 TypeError가 난다."""
    temperature: float
    seed: int
    top_k: int
    top_p: float
    repeat_penalty: float
    num_predict: int
    num_ctx: int

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"LlmOptions.{f.name}은 숫자여야 한다: {v!r}")

    def as_dict(self) -> dict:
        """Ollama options 객체. 7개를 항상 모두 담는다."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class LlmResult:
    ok: bool
    reason: str | None = None            # 실패 이유 코드. 정상이면 None
    text: str = ""
    done_reason: str | None = None
    load_ms: float | None = None         # 아래 *_ms 와 *_count 는 Ollama가 준 값(ns는 ms로 바꿈). 안 주면 None
    prompt_eval_count: int | None = None
    prompt_eval_cached_count: int | None = None
    prompt_eval_ms: float | None = None
    eval_count: int | None = None
    eval_ms: float | None = None
    total_ms: float | None = None
    wall_ms: float = 0.0                 # 우리가 잰 전체 시간
    http_status: int | None = None
    request: dict = field(default_factory=dict)   # 보낸 옵션·모델·keep_alive 요약. 프롬프트 본문은 넣지 않는다


class _Fail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Deadline(Exception):
    """총 시간 상한을 넘겼다."""


def validate_base_url(url: str) -> str:
    """http://host:port 형식만 허용한다(경로·계정·질의 없음). 설정을 읽을 때 한 번 부른다."""
    m = _BASE_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if m is None or not 1 <= int(m.group(1)) <= 65535:
        raise ValueError(f"http://host:port 형식이어야 한다: {url!r}")
    return url


def _left(deadline: float, clock) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise _Deadline
    return remaining


def _abort(sock: socket.socket, fired: threading.Event) -> None:
    """마감 시각에 소켓을 끊는다. 헤더나 본문을 조금씩 흘려 보내는 서버에도 총 시간을 지키기 위한 안전장치."""
    fired.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _read_body(sock: socket.socket, resp: http.client.HTTPResponse, deadline: float, clock) -> bytes:
    parts: list[bytes] = []
    size = 0
    while not resp.isclosed():                       # 본문을 다 읽으면 응답과 소켓이 닫히므로 그때 멈춘다
        sock.settimeout(_left(deadline, clock))      # 청크마다 남은 시간으로 줄인다
        chunk = resp.read1(65536)                    # read()는 버퍼가 찰 때까지 막히므로 read1을 쓴다
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _Fail("bad_response")
        parts.append(chunk)
    if resp.length:                                  # Content-Length만큼 못 받고 끊겼다
        raise http.client.IncompleteRead(b"".join(parts), resp.length)
    return b"".join(parts)


def _request(base_url: str, method: str, path: str, data: bytes | None,
             connect_timeout: float, total_timeout: float, clock, start: float) -> tuple[int, bytes]:
    """(HTTP 상태, 본문). 실패는 _Fail(이유 코드).
    연결 뒤 끊김(reset, 응답 없이 종료, 본문 중간 끊김)은 connect_error로 본다."""
    u = urllib.parse.urlsplit(base_url)
    deadline = start + total_timeout
    # 호스트 이름 조회(DNS)는 소켓 제한이 닿지 않는다. OLLAMA_URL은 IP 주소를 쓰는 것을 전제로 한다.
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=min(connect_timeout, total_timeout))
    timer = None
    try:
        try:
            conn.connect()
        except OSError as e:                         # 연결 거부, 연결 시간 초과 포함
            raise _Fail("connect_error") from e
        sock = conn.sock                             # 응답이 Connection: close면 conn.sock이 비므로 미리 잡아 둔다
        fired = threading.Event()
        timer = threading.Timer(max(0.0, deadline - clock()), _abort, args=(sock, fired))
        timer.daemon = True
        timer.start()
        try:
            sock.settimeout(_left(deadline, clock))
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, _read_body(sock, resp, deadline, clock)
        except (TimeoutError, _Deadline) as e:
            raise _Fail("timeout") from e
        except (OSError, http.client.HTTPException) as e:
            raise _Fail("timeout" if fired.is_set() or clock() >= deadline else "connect_error") from e
        if fired.is_set():                           # 감시 타이머가 소켓을 끊어 본문이 조용히 짧아졌을 수 있다
            raise _Fail("timeout")
        return status, raw
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _ms(v) -> float | None:
    return None if isinstance(v, bool) or not isinstance(v, int | float) else round(v / _NS_PER_MS, 3)


def _count(v) -> int | None:
    return None if isinstance(v, bool) or not isinstance(v, int) else v


def _interpret(status: int, raw: bytes) -> LlmResult:
    if status != 200:
        if status == 404 and b"not found" in raw.lower():
            return LlmResult(ok=False, reason="model_missing")
        return LlmResult(ok=False, reason=f"http_{status}")
    try:
        obj = json.loads(raw)
    except ValueError:
        return LlmResult(ok=False, reason="bad_json")
    msg = obj.get("message") if isinstance(obj, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str):
        return LlmResult(ok=False, reason="bad_response")
    done_reason = obj.get("done_reason")
    res = LlmResult(
        ok=True,
        text=content,
        done_reason=done_reason if isinstance(done_reason, str) else None,
        load_ms=_ms(obj.get("load_duration")),
        prompt_eval_count=_count(obj.get("prompt_eval_count")),
        prompt_eval_cached_count=_count(obj.get("prompt_eval_cached_count")),
        prompt_eval_ms=_ms(obj.get("prompt_eval_duration")),
        eval_count=_count(obj.get("eval_count")),
        eval_ms=_ms(obj.get("eval_duration")),
        total_ms=_ms(obj.get("total_duration")),
    )
    if res.done_reason == "length":                  # 잘린 답은 믿지 않는다. 글과 통계는 그대로 돌려준다
        res.ok, res.reason = False, "truncated"
    return res


def chat(base_url: str, model: str, messages: list, options: LlmOptions, fmt: dict | None = None,
         keep_alive: str = "24h", connect_timeout: float = 5.0, total_timeout: float = 30.0,
         clock=time.monotonic) -> LlmResult:
    """/api/chat을 한 번 부른다. 어떤 실패에도 예외를 내지 않는다.
    total_timeout은 연결부터 본문 끝까지의 총 상한이다."""
    start = clock()
    request: dict = {}
    status = None
    try:
        request = {"model": model, "keep_alive": keep_alive, "options": options.as_dict(),
                   "format": None if fmt is None else "json_schema", "message_count": len(messages)}
        body = {"model": model, "messages": messages, "stream": False,
                "options": options.as_dict(), "keep_alive": keep_alive}
        if fmt is not None:
            body["format"] = fmt
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, raw = _request(base_url, "POST", "/api/chat", data, connect_timeout, total_timeout, clock, start)
        result = _interpret(status, raw)
    except _Fail as e:
        result = LlmResult(ok=False, reason=e.reason)
    except Exception as e:                           # 감시 루프를 절대 막지 않는다
        result = LlmResult(ok=False, reason=f"internal_error:{type(e).__name__}")
    result.http_status = status
    result.request = request
    result.wall_ms = round((clock() - start) * 1000, 3)
    return result


def version(base_url: str, timeout: float = 5) -> str | None:
    """Ollama 버전 문자열. 실패하면 None(예외 없음)."""
    try:
        now = time.monotonic
        status, raw = _request(base_url, "GET", "/api/version", None, timeout, timeout, now, now())
        v = json.loads(raw).get("version") if status == 200 else None
        return v if isinstance(v, str) else None
    except Exception:
        return None
