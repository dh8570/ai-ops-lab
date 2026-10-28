"""엔드포인트: 가짜 Ollama(로컬 스레드 서버)로 스트림, 기록, 409, 자물쇠 해제, probe 판정을 확인한다."""
import json

import pytest

import app as appmod


@pytest.fixture
def client(monkeypatch, tmp_path, fake_ollama, tk):
    monkeypatch.setattr(appmod, "OLLAMA_URL", fake_ollama.url)
    monkeypatch.setattr(appmod, "TRACE_PATH", str(tmp_path / "traces.jsonl"))
    monkeypatch.setattr(appmod, "get_tokenizer", lambda: tk)
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        c.trace_path = tmp_path / "traces.jsonl"
        yield c
    assert not appmod._busy.locked()                   # 어떤 테스트도 자물쇠를 남기지 않아야 한다


def run_body(**kw):
    return {"prompt": "안녕", "model": "fake:1b", **kw}


def events(resp):
    """응답을 끝까지 읽고 닫는다. 실제 WSGI 서버는 응답이 끝나면 close()를 부르고, 자물쇠는 그때 풀린다."""
    try:
        return [json.loads(line) for line in resp.get_data(as_text=True).splitlines() if line.strip()]
    finally:
        resp.close()


def test_run_streams_events_and_writes_trace(client, fake_ollama):
    resp = client.post("/api/run", json=run_body(seed=7, num_predict=3))
    assert resp.status_code == 200 and resp.mimetype == "application/x-ndjson"
    ev = events(resp)
    assert [e["type"] for e in ev] == ["start", "chunk", "chunk", "chunk", "done"]
    assert ev[0]["local_tokens"] > 0 and ev[0]["options"]["seed"] == 7
    assert "".join(e["text"] for e in ev if e["type"] == "chunk") == "안녕하세요"
    assert ev[-1]["joined"] == "안녕하세요" and ev[-1]["final_response"] == ""
    assert ev[-1]["stats"]["prompt_eval_count"] == 31
    sent = fake_ollama.generate_requests[-1]
    assert sent["stream"] is True and sent["options"]["seed"] == 7 and sent["options"]["num_predict"] == 3
    trace = json.loads(client.trace_path.read_text(encoding="utf-8").splitlines()[-1])
    assert trace["status"] == "ok" and trace["answer"] == "안녕하세요" and trace["prompt"] == "안녕"
    assert trace["chunks"] == 3 and trace["stats"]["eval_count"] == 3 and trace["local_tokens"] == ev[0]["local_tokens"]


def test_lock_is_released_after_a_run_so_the_next_run_works(client):
    assert events(client.post("/api/run", json=run_body()))[-1]["type"] == "done"
    assert events(client.post("/api/run", json=run_body()))[-1]["type"] == "done"


def test_409_while_busy(client):
    assert appmod._busy.acquire(blocking=False)
    try:
        assert client.post("/api/run", json=run_body()).status_code == 409
        assert client.get("/api/probe?model=fake:1b").status_code == 409
    finally:
        appmod._busy.release()


def test_lock_released_when_client_disconnects_midstream(client):
    resp = client.post("/api/run", json=run_body(), buffered=False)
    next(iter(resp.response))                           # 첫 사건(start)만 받고
    resp.close()                                        # 브라우저가 끊은 것과 같다
    assert not appmod._busy.locked()
    last = json.loads(client.trace_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["status"] == "aborted"


def test_invalid_input_is_400_and_does_not_reach_ollama(client, fake_ollama):
    for body in (run_body(num_predict=513), run_body(model="nope"), {"prompt": "가" * 8001, "model": "fake:1b"},
                 run_body(num_ctx=1000)):
        assert client.post("/api/run", json=body).status_code == 400
    assert client.post("/api/run", data="not json", content_type="application/json").status_code == 400
    assert fake_ollama.generate_requests == []


def test_tokenize_endpoint(client):
    r = client.post("/api/tokenize", json={"text": "꿻 hello"})
    body = r.get_json()
    assert r.status_code == 200 and body["bytes_match"] is True and body["partial_count"] == 2
    assert client.post("/api/tokenize", json={"text": "x" * 8001}).status_code == 400


@pytest.mark.parametrize("mode,verdict", [("supported", "supported"), ("no_field", "no_field"), ("error", "error")])
def test_probe_verdicts(client, fake_ollama, mode, verdict):
    fake_ollama.logprobs = mode
    r = client.get("/api/probe?model=fake:1b")
    body = r.get_json()
    assert r.status_code == 200 and body["verdict"] == verdict
    sent = fake_ollama.generate_requests[-1]
    assert sent["logprobs"] is True and sent["top_logprobs"] == 5 and sent["options"]["num_predict"] == 3
    if verdict == "supported":
        assert body["first_entry"]["token"] == "a" and body["has_top_logprobs"] is True   # 응답에 있던 값만 돌려준다
    if verdict == "error":
        assert "unknown field" in body["error"]


def test_show_and_config(client):
    assert client.get("/api/show?model=fake:1b").get_json()["template"].startswith("<tpl>")
    assert client.get("/api/show?model=other").status_code == 400
    cfg = client.get("/api/config").get_json()
    assert cfg["models"] == ["fake:1b"] and cfg["limits"]["prompt_max"] == 8000
    assert cfg["tokenizer"]["revision"] and "ollama_url" not in json.dumps(cfg)


def test_index_has_no_external_resources(client):
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<html lang=\"ko\">" in html
    html = html.replace("http://www.w3.org/2000/svg", "")        # SVG 네임스페이스 식별자일 뿐 요청 주소가 아니다
    assert "http://" not in html and "https://" not in html and "//cdn" not in html   # CDN·외부 글꼴 없음
    assert "connect-src 'self'" in r.headers["Content-Security-Policy"]
