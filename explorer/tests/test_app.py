"""엔드포인트: 가짜 Ollama(로컬 스레드 서버)로 스트림, 기록, 409, 자물쇠 해제, probe 판정을 확인한다."""
import json
import re
from pathlib import Path

import pytest

import app as appmod


@pytest.fixture
def client(monkeypatch, tmp_path, fake_ollama, tk):
    monkeypatch.setattr(appmod, "OLLAMA_URL", fake_ollama.url)
    monkeypatch.setattr(appmod, "DATA_DIR", str(tmp_path))      # 실행 기록(runs/, index.json)이 쌓이는 곳
    monkeypatch.setattr(appmod, "get_tokenizer", lambda: tk)
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        c.data_dir = tmp_path
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
    assert ev[0]["run_id"] == ev[-1]["run_id"]                  # start와 done에 같은 run_id
    rec = json.loads((client.data_dir / "runs" / f"{ev[0]['run_id']}.json").read_text(encoding="utf-8"))
    st = {s["id"]: s for s in rec["stages"]}
    assert rec["status"] == "ok" and st["input"]["output"]["prompt"] == "안녕"
    assert st["restore"]["output"]["server_joined"] == "안녕하세요"
    assert st["decode"]["output"]["chunk_count"] == 3 and st["decode"]["output"]["eval_count"] == 3
    assert st["prefill"]["output"]["local_token_count"] == ev[0]["local_tokens"]


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
    start = json.loads(next(iter(resp.response)))       # 첫 사건(start)만 받고
    resp.close()                                        # 브라우저가 끊은 것과 같다
    assert not appmod._busy.locked()
    rec = json.loads((client.data_dir / "runs" / f"{start['run_id']}.json").read_text(encoding="utf-8"))
    assert rec["status"] == "aborted"


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


# ---------------------------------------------------------------- 척추: 읽기 경로와 정적 파일 규칙
STATIC = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'")


def _do_run(client):
    ev = events(client.post("/api/run", json=run_body()))
    return ev[0]["run_id"]


def test_data_files_after_a_run(client):
    run_id = _do_run(client)
    idx = client.get("/data/index.json")
    assert idx.status_code == 200 and idx.mimetype == "application/json"
    assert idx.get_json()[0]["run_id"] == run_id and idx.get_json()[0]["status"] == "ok"
    r = client.get(f"/data/runs/{run_id}.json")
    body = r.get_json()
    assert r.status_code == 200 and body["run_id"] == run_id and body["status"] == "ok"
    assert [s["id"] for s in body["stages"]] == ["input", "tokenize", "load", "prefill", "decode", "restore"]
    assert all(s["status"] == "ok" for s in body["stages"])


def test_index_is_an_empty_list_before_any_run(client):
    r = client.get("/data/index.json")
    assert r.status_code == 200 and r.get_json() == []


BAD_RUN_IDS = ["R-20261028-064512-AB12", "r-2026102-064512-ab12", "r-20261028-064512-ab1", "r-20261028-064512-ab123",
               "..%2Findex", "..%2F..%2Fetc%2Fpasswd", "x", "r-20261028-064512-zzzz"]


@pytest.mark.parametrize("bad", BAD_RUN_IDS)
def test_invalid_run_ids_are_404(client, bad):
    assert client.get(f"/data/runs/{bad}.json").status_code == 404


def test_other_files_under_data_are_404(client):
    # 파일이 실제로 있어도 404여야 한다
    (client.data_dir / "traces.jsonl").write_text('{"secret": "old trace"}\n', encoding="utf-8")
    for path in ("/data/traces.jsonl", "/data/", "/data/runs/", "/data/runs/index.json", "/data/../app.py"):
        assert client.get(path).status_code == 404, path


def test_csp_header_is_the_strict_one(client):
    for path in ("/", "/data/index.json", "/content/stages.json", "/js/app.js"):
        assert client.get(path).headers["Content-Security-Policy"] == CSP, path
    assert "unsafe-inline" not in CSP


def test_static_routes_serve_only_js_css_json(client):
    assert client.get("/js/app.js").mimetype == "text/javascript"
    assert client.get("/css/site.css").mimetype == "text/css"
    assert client.get("/content/stages.json").mimetype == "application/json"
    assert client.get("/js/views/home.js").status_code == 200           # 하위 폴더도 된다
    for path in ("/js/app.js.map", "/js/../app.py", "/js/..%2Fapp.py", "/content/..%2F..%2Fapp.py",
                 "/css/missing.css", "/js/", "/static/index.html"):
        assert client.get(path).status_code == 404, path


def _static_files(*suffixes):
    return [p for p in STATIC.rglob("*") if p.is_file() and p.suffix in suffixes]


def test_index_html_has_no_inline_script_or_event_attributes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
    # 인라인 스크립트 내용이 없다(모든 script는 src를 가진다)
    assert scripts and all('src="' in attrs and not body.strip() for attrs, body in scripts)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)                                              # on*= 속성 없음
    # 인라인 스타일이 없다
    assert not re.search(r"<style\b", html, re.I) and not re.search(r"\sstyle\s*=", html, re.I)
    assert '<script type="module" src="./js/app.js">' in html and '<script src="./js/theme.js">' in html


def test_theme_js_is_short_classic_script():
    src = (STATIC / "js" / "theme.js").read_text(encoding="utf-8")
    assert len(src.splitlines()) <= 20 and not re.search(r"^\s*(import|export)\b", src, re.M)


def test_all_static_urls_are_relative():
    # SVG 네임스페이스 식별자(요청 주소가 아님). createElementNS에 필요해서 이것만 제외한다
    ns = "http://www.w3.org/2000/svg"
    for p in _static_files(".html", ".js", ".css"):
        text = p.read_text(encoding="utf-8").replace(ns, "")
        assert not re.search(r"https?://", text), f"{p.name}: http(s):// 주소"
        assert not re.search(r"""(?:href|src|action)\s*=\s*["']/(?!/)""", text), f"{p.name}: /로 시작하는 주소"
        assert not re.search(r"""url\(\s*["']?(?:/|https?:)""", text), f"{p.name}: CSS url()"
        for m in re.finditer(r"""\b(?:href|src)\s*=\s*"([^"]+)""", text):                    # HTML 속성
            assert m.group(1).startswith(("./", "#")), f"{p.name}: {m.group(1)}"
        for m in re.finditer(r"""\b(?:href|src)\s*[:=]\s*[`'"]([^`'"]+)""", text):           # JS 속성 대입·객체 키
            assert m.group(1).startswith(("./", "#")), f"{p.name}: {m.group(1)}"
        for m in re.finditer(r"""\bfetch\(\s*[`'"]([^`'"]+)""", text):
            assert m.group(1).startswith("./"), f"{p.name}: fetch {m.group(1)}"
        for m in re.finditer(r"""\bfrom\s+['"]([^'"]+)['"]""", text):                         # ES 모듈 import
            assert m.group(1).startswith(("./", "../")), f"{p.name}: import {m.group(1)}"


def test_static_js_avoids_forbidden_apis():
    banned = [r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"setAttribute\(\s*['\"]style['\"]",
              r"\beval\(", r"document\.write", r"new Function\(", r"\.on[a-z]+\s*=\s*"]
    for p in _static_files(".js"):
        text = p.read_text(encoding="utf-8")
        for pat in banned:
            assert not re.search(pat, text), f"{p.relative_to(STATIC)}: {pat}"


def test_first_gap_bar_does_not_use_the_load_color():
    css = (STATIC / "css" / "site.css").read_text(encoding="utf-8")
    rule = re.search(r"svg\.gap \.bar-first \{([^}]*)\}", css).group(1)
    assert "fill: none" in rule and "seg-load" not in rule                     # 윤곽선만 있는 막대, 올리기 색 없음
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "올리기·읽기 시간 섞임" in ui and "first-label" in ui                # 막대 위에 직접 다는 라벨


def test_five_evidence_grades_have_labels_and_styles():
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "site.css").read_text(encoding="utf-8")
    labels = (("measured", "실측"), ("computed", "계산"), ("illustrative", "설명용"),
              ("unverified", "미확인"), ("input", "입력"))
    for grade, label in labels:
        assert f"{grade}: ['{label}'" in ui and f".ev-{grade} " in css, grade


def test_time_bar_keeps_narrow_segments_visible_and_explains_the_emphasis():
    ui = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
    assert "seg.style.minWidth = '2px'" in ui
    assert "진하게 = 지금 보는 단계, 옅게 = 다른 단계" in ui


def test_input_stage_settings_rows_are_always_the_input_grade():
    src = (STATIC / "js" / "render" / "input.js").read_text(encoding="utf-8")
    assert "], { '*': 'input' }));" in src                       # 설정 행: 기록의 evidence와 무관하게 항상 입력
    assert "], stage.evidence));" in src                         # prompt 행: 기록에 적힌 등급 그대로
