"""T8c: 장애 분석 기록(kind "incident")을 탐험 사이트가 읽고 보여 주는 데 필요한 것.
검증기 완화(tokenizer), 합성 장애 기록 fixture, 단계 content, 화면 소스 규칙. 기존 시험 파일은 수정하지 않았다."""
import copy
import json
import re
from pathlib import Path

import pytest

import app as appmod
import runs

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "static"
CONTENT = STATIC / "content"
FIXTURE = HERE / "fixtures" / "incident_run.json"
INCIDENT_STAGES = ("evidence", "prompt", "llm_call", "parse", "notify")     # watcher/llm_trace.STAGE_IDS와 같다
TOKENIZER = {"repo": "Qwen/Qwen2.5-7B-Instruct", "revision": "a" * 40, "sha256": "b" * 64}


def incident():
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- fixture
def test_fixture_is_a_valid_synthetic_incident_record_with_five_stages():
    rec = incident()
    assert runs.validate_bundle(rec) == []
    assert rec["kind"] == "incident" and rec["schema_version"] == 1 and rec["exportable"] is False
    assert [s["id"] for s in rec["stages"]] == list(INCIDENT_STAGES)
    assert "tokenizer" not in rec["environment"]
    assert "합성" in rec["redaction"]["note"] and "합성" in rec["title"]


def test_fixture_has_no_addresses_or_secrets():
    text = FIXTURE.read_text(encoding="utf-8")
    assert not re.search(r"https?://|hooks\.slack|[0-9]{1,3}(\.[0-9]{1,3}){3}|password|passwd|secret|bearer|api[_-]?key",
                         text, re.I)


# ---------------------------------------------------------------- 검증기: tokenizer는 놀이터에서만 필수
def test_incident_record_without_tokenizer_passes():
    assert runs.validate_bundle(incident()) == []


def test_playground_record_still_requires_tokenizer():
    rec = incident()
    rec["kind"] = "playground"                                  # 같은 환경 객체로 놀이터 기록을 흉내 낸다
    assert any("environment.tokenizer" in p for p in runs.validate_bundle(rec))


def test_incident_tokenizer_is_optional_but_checked_when_present():
    rec = incident()
    rec["environment"]["tokenizer"] = dict(TOKENIZER)
    assert runs.validate_bundle(rec) == []
    rec["environment"]["tokenizer"] = {"repo": "x"}             # 있으면 같은 형식으로 검사한다
    assert any("environment.tokenizer" in p for p in runs.validate_bundle(rec))
    rec["environment"]["tokenizer"] = None
    assert any("environment.tokenizer" in p for p in runs.validate_bundle(rec))


def test_other_rules_still_apply_to_incident_records():
    for mutate, expect in [(lambda r: r.update(schema_version=2), "schema_version"),
                           (lambda r: r.update(run_id="R-1"), "run_id 모양"),
                           (lambda r: r["environment"].pop("model"), "environment.model"),
                           (lambda r: r["stages"][0]["evidence"].update({"x": "guess"}), "모르는 evidence 등급")]:
        rec = incident()
        mutate(rec)
        assert any(expect in p for p in runs.validate_bundle(rec)), expect


def test_incident_record_is_written_and_listed_and_served(monkeypatch, tmp_path):
    assert runs.write_run(tmp_path, incident()) is True
    idx = runs.read_index(tmp_path)
    assert [(e["run_id"], e["kind"], e["model"]) for e in idx] == [("r-20261117-030000-a1b2", "incident", "qwen2.5:7b")]
    monkeypatch.setattr(appmod, "DATA_DIR", str(tmp_path))
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        r = c.get("/data/runs/r-20261117-030000-a1b2.json")
        assert r.status_code == 200 and [s["id"] for s in r.get_json()["stages"]] == list(INCIDENT_STAGES)
        assert c.get("/data/index.json").get_json()[0]["kind"] == "incident"


# ---------------------------------------------------------------- content: 단계 id마다 content가 있다
def test_every_incident_and_playground_stage_has_a_listed_content_file():
    stages = {s["id"]: s for s in json.loads((CONTENT / "stages.json").read_text(encoding="utf-8"))}
    for sid in (*INCIDENT_STAGES, *runs.PLAYGROUND_STAGES):
        assert sid in stages, f"stages.json에 {sid}가 없음(단계 페이지가 '알 수 없는 단계'로 실패)"
        d = json.loads((CONTENT / "stages" / f"{sid}.json").read_text(encoding="utf-8"))
        assert d["summary"].strip(), sid
    # 장애 분석 5단계: 모두 구현됨이고 쉬운 말·정확하게·실패·미확인이 모두 있다
    for sid in INCIDENT_STAGES:
        s, d = stages[sid], json.loads((CONTENT / "stages" / f"{sid}.json").read_text(encoding="utf-8"))
        assert s["implemented"] is True and s["applies_to"] == ["incident"]
        assert d["easy"].strip() and d["precise"].strip() and d["failures"] and d["unverified"], sid


def test_new_stage_content_states_the_facts_checked_in_the_code():
    def load(sid):
        return json.loads((CONTENT / "stages" / f"{sid}.json").read_text(encoding="utf-8"))

    call = load("llm_call")["precise"]
    assert "옵션 7개" in call and "5초" in call and "30초" in call and "wall_ms" in call
    ev = load("evidence")
    assert "예외 메시지" in ev["easy"] and "메시지는 어디에도 남기지 않습니다" in ev["precise"]
    assert "foreign_script" in load("parse")["precise"] and "unknown_marker" in load("parse")["precise"]
    # 확인하지 못한 내용은 단정하지 않고 가설이라고 밝힌다(evidence의 첫 항목은 코드를 읽은 가설)
    assert "(가설)" in ev["unverified"][0]
    # prompt: system은 해시와 글자 수만 기록, mask는 user에만 적용되고 지금은 항등, 버전, 추정, 줄이기
    p = load("prompt")
    assert "system_sha256" in p["precise"] and "user 글에만 적용" in p["precise"] and "v1이 기본" in p["precise"]
    assert "× 1.0" in p["precise"] and "경로 3개, 오류 유형 2개" in p["precise"]
    assert any("system 전문이 기록에 없어서" in t for t in p["failures"])
    assert any("채점표" in t for t in p["unverified"])
    # notify: 재시도, 상태 셋, 복구 후 도착 표시, 채널은 기록에 없음, 웹훅 주소 값은 없음
    n = load("notify")
    assert "3번 시도" in n["precise"] and "5초" in n["precise"] and "dryrun" in n["precise"]
    assert "skipped" in n["precise"] and "(복구 후 도착)" in n["precise"]
    assert n["unverified"][0] == "기록에는 알림이 어느 채널(운영 또는 분석)로 갔는지가 없고 이벤트 로그에만 있다."
    assert not re.search(r"https?://|hooks\.slack", json.dumps(n, ensure_ascii=False))


# ---------------------------------------------------------------- 화면 소스 규칙
def read_js(*parts):
    return (STATIC / "js" / Path(*parts)).read_text(encoding="utf-8")


def test_stage_view_has_a_renderer_for_every_incident_stage_and_shows_recorded_stages_even_if_planned():
    src = read_js("views", "stage.js")
    for sid in INCIDENT_STAGES:
        assert f"{sid}: incident" in src, sid
    # 지도에 예정이어도 기록에 있으면 보여 준다(prompt, notify)
    assert "const planned = !meta.implemented && !stage;" in src


def test_home_shows_the_reports_link_only_in_static_mode():
    src = read_js("views", "home.js")
    guard = src.index("if (state.mode !== 'live') {")
    link = src.index("./reports/index.html")
    assert guard < link and src.count("./reports/index.html") == 1
    assert src.index("\n  }", guard) > link                              # 링크는 그 if 블록 안에 있다


def test_home_does_not_assume_playground_records():
    src = read_js("views", "home.js")
    assert "index.find((e) => e.kind === 'playground')" not in src     # 놀이터 기록만 가정하던 줄이 없다
    assert "latestFor" in src and "index[0]" in src


def test_incident_renderer_uses_text_only_and_handles_missing_values():
    src = read_js("render", "incident.js")
    assert "class: 'block'" in src and "el('details'" in src
    assert "innerHTML" not in src and "없음" in src
    assert re.search(r"String\(null\)|String\(undefined\)", src) is None


@pytest.mark.parametrize("name", list(INCIDENT_STAGES))
def test_new_content_files_have_exactly_the_required_keys(name):
    d = json.loads((CONTENT / "stages" / f"{name}.json").read_text(encoding="utf-8"))
    assert set(d) == {"summary", "easy", "precise", "failures", "unverified"}
