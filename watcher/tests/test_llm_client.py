import json
import socket

import pytest
from fake_ollama import FakeOllama

import llm_client
from llm_client import LlmOptions, chat, validate_base_url, version

OPTS = LlmOptions(temperature=0, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0, num_predict=512, num_ctx=4096)
SECRET = "프롬프트-본문-마커-7f3a"
MSGS = [{"role": "system", "content": "시스템"}, {"role": "user", "content": SECRET}]
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def _closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _call(url, **kw):
    kw.setdefault("total_timeout", 5.0)
    return chat(url, "qwen2.5:7b", MSGS, OPTS, **kw)


def test_ok_maps_ollama_stats_to_ms():
    with FakeOllama("ok") as fake:
        r = _call(fake.url)
    assert r.ok is True and r.reason is None
    assert r.text.startswith('{"summary"')
    assert r.done_reason == "stop" and r.http_status == 200
    assert (r.load_ms, r.prompt_eval_ms, r.eval_ms, r.total_ms) == (1000.0, 300.0, 1000.0, 2500.0)
    assert (r.prompt_eval_count, r.prompt_eval_cached_count, r.eval_count) == (50, 32, 40)
    assert r.wall_ms > 0


def test_request_body_always_has_all_options_keep_alive_and_stream_false():
    with FakeOllama("ok") as fake:
        _call(fake.url)
        _call(fake.url, fmt=SCHEMA, keep_alive="5m")
        first, second = (json.loads(q["body"]) for q in fake.requests)
        assert all(q["path"] == "/api/chat" and q["method"] == "POST" for q in fake.requests)
    for body in (first, second):
        assert body["stream"] is False
        assert body["model"] == "qwen2.5:7b" and body["messages"] == MSGS
        assert set(body["options"]) == {
            "temperature", "seed", "top_k", "top_p", "repeat_penalty", "num_predict", "num_ctx"}
        assert body["options"] == OPTS.as_dict()
    assert first["keep_alive"] == "24h" and "format" not in first
    assert second["keep_alive"] == "5m" and second["format"] == SCHEMA


def test_options_cannot_be_built_with_a_missing_or_invalid_field():
    with pytest.raises(TypeError):
        LlmOptions(temperature=0, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0, num_predict=512)
    with pytest.raises(ValueError):
        LlmOptions(temperature=None, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0, num_predict=512, num_ctx=4096)
    with pytest.raises(ValueError):
        LlmOptions(temperature=True, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0, num_predict=512, num_ctx=4096)


@pytest.mark.parametrize("mode,reason", [
    ("http500", "http_500"),
    ("notfound", "model_missing"),
    ("garbage", "bad_json"),
    ("nocontent", "bad_response"),
    ("drop", "connect_error"),
])
def test_failure_reason_codes(mode, reason):
    with FakeOllama(mode) as fake:
        r = _call(fake.url)
    assert r.ok is False and r.reason == reason
    assert r.text == "" and r.wall_ms > 0


def test_http_status_is_recorded_on_http_failures():
    with FakeOllama("http500") as fake:
        assert _call(fake.url).http_status == 500
    with FakeOllama("notfound") as fake:
        assert _call(fake.url).http_status == 404


def test_truncated_is_not_ok_but_keeps_text_and_stats():
    with FakeOllama("truncated") as fake:
        r = _call(fake.url)
    assert r.ok is False and r.reason == "truncated" and r.done_reason == "length"
    assert r.text.startswith('{"summary"')
    assert r.eval_count == 40 and r.total_ms == 2500.0


def test_connection_refused():
    r = _call(_closed_port_url())
    assert r.ok is False and r.reason == "connect_error" and r.http_status is None


def test_slow_server_hits_total_timeout():
    with FakeOllama("slow", delay=10) as fake:
        r = _call(fake.url, total_timeout=0.5)
    assert r.reason == "timeout" and r.ok is False
    assert 400 <= r.wall_ms < 2000


def test_slowdrip_cannot_exceed_total_timeout():
    # 한 바이트씩 0.2초 간격(본문 80여 바이트 → 15초 이상).
    # 소켓 읽기 제한(1초)에는 매번 안 걸리지만 총 상한에는 걸려야 한다.
    with FakeOllama("slowdrip", interval=0.2) as fake:
        r = _call(fake.url, total_timeout=1.0)
    assert r.ok is False and r.reason == "timeout"
    assert r.wall_ms < 2500


def test_slowdrip_also_stops_a_dripped_error_body_at_deadline():
    with FakeOllama("slowdrip", interval=0.2) as fake:
        assert version(fake.url, timeout=1.0) is None


def test_no_exception_escapes():
    # 직렬화할 수 없는 메시지
    r = chat("http://127.0.0.1:1", "m", [object()], OPTS)
    assert r.ok is False and r.reason == "internal_error:TypeError"
    # 옵션이 LlmOptions가 아님
    r = chat("http://127.0.0.1:1", "m", MSGS, None)
    assert r.ok is False and r.reason == "internal_error:AttributeError"


def test_unexpected_exception_inside_request_is_reported(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(llm_client.http.client, "HTTPConnection", boom)
    r = _call("http://127.0.0.1:1")
    assert r.ok is False and r.reason == "internal_error:RuntimeError"
    assert r.request["model"] == "qwen2.5:7b"


def test_request_summary_has_options_but_never_the_prompt():
    with FakeOllama("ok") as fake:
        r = _call(fake.url, fmt=SCHEMA)
    assert r.request["options"] == OPTS.as_dict()
    assert r.request["model"] == "qwen2.5:7b" and r.request["keep_alive"] == "24h"
    assert SECRET not in json.dumps(r.request, ensure_ascii=False)
    assert "시스템" not in json.dumps(r.request, ensure_ascii=False)


@pytest.mark.parametrize("url", ["http://10.97.0.1:11434", "http://localhost:80", "http://ollama.local:11434"])
def test_validate_base_url_accepts(url):
    assert validate_base_url(url) == url


@pytest.mark.parametrize("url", [
    "", None, "http://host", "https://host:1", "http://host:1/", "http://host:1/api", "http://u:p@host:1",
    "http://host:0", "http://host:99999", "http://host:1\n", "ftp://host:1", "host:1", "http://:1",
    "http://host:1?x=1",
])
def test_validate_base_url_rejects(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_version_ok_and_failures():
    with FakeOllama("ok") as fake:
        assert version(fake.url) == "0.40.2"
    for mode in ("http500", "garbage", "drop"):
        with FakeOllama(mode) as fake:
            assert version(fake.url) is None
    assert version(_closed_port_url()) is None
    assert version("not a url") is None
