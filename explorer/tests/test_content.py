"""화면 설명 content 파일: stages.json과 stages/<id>.json의 구조, 구현·예정 단계의 필수 내용, 확정 문구."""
import json
from pathlib import Path

import pytest
from expected_help import OPT_HELP

import runs

CONTENT = Path(__file__).resolve().parent.parent / "static" / "content"
STAGES = json.loads((CONTENT / "stages.json").read_text(encoding="utf-8"))
KEYS = {"summary", "easy", "precise", "failures", "unverified"}
IDS = [s["id"] for s in STAGES]


def load(sid):
    return json.loads((CONTENT / "stages" / f"{sid}.json").read_text(encoding="utf-8"))


def test_stage_ids_are_unique_and_fields_are_valid():
    assert len(IDS) == len(set(IDS))
    for s in STAGES:
        assert set(s) == {"id", "title", "lane", "week", "implemented", "applies_to"}, s
        assert s["lane"] in ("A", "B") and s["week"] in ("A", "B") and isinstance(s["implemented"], bool)
        assert isinstance(s["applies_to"], list) and s["title"].strip()


def test_every_stage_has_a_content_file_and_every_content_file_is_listed():
    files = sorted(p.stem for p in (CONTENT / "stages").glob("*.json"))
    assert files == sorted(IDS)


@pytest.mark.parametrize("sid", IDS)
def test_content_has_required_keys(sid):
    d = load(sid)
    assert set(d) == KEYS
    assert isinstance(d["summary"], str) and isinstance(d["easy"], str) and isinstance(d["precise"], str)
    assert isinstance(d["failures"], list) and isinstance(d["unverified"], list)
    assert all(isinstance(t, str) and t.strip() for t in d["failures"] + d["unverified"])


@pytest.mark.parametrize("s", [s for s in STAGES if s["implemented"]], ids=lambda s: s["id"])
def test_implemented_stage_has_easy_and_precise(s):
    d = load(s["id"])
    assert d["summary"].strip() and d["easy"].strip() and d["precise"].strip()


@pytest.mark.parametrize("s", [s for s in STAGES if not s["implemented"]], ids=lambda s: s["id"])
def test_planned_stage_has_only_a_summary(s):
    d = load(s["id"])
    assert d["summary"].strip()
    assert d["easy"] == "" and d["precise"] == "" and d["failures"] == [] and d["unverified"] == []


def test_stage_map_matches_the_playground_record_stages():
    impl = [s["id"] for s in STAGES if s["implemented"]]
    assert impl == list(runs.PLAYGROUND_STAGES)                       # 구현된 단계 = 놀이터 기록의 6단계(같은 순서)
    assert all(s["applies_to"] == ["playground"] for s in STAGES if s["implemented"])
    planned_a = [s["id"] for s in STAGES if not s["implemented"] and s["week"] == "A"]
    planned_b = [s["id"] for s in STAGES if not s["implemented"] and s["week"] == "B"]
    assert planned_a == ["log_emit", "select", "mask", "prompt", "notify", "store"]
    assert planned_b == ["chunk", "embed", "retrieve"] and all(s["lane"] == "B" for s in STAGES if s["id"] in planned_b)


def test_prefill_texts_are_the_pm_final_wording():
    d = load("prefill")
    assert d["precise"].startswith("입력 토큰 전체를 한 번의 병렬 계산으로 처리해 KV 캐시를 채우고")
    assert "72 = 우리 글 43 + 틀 29입니다(실측)" in d["precise"]
    assert "뒤 5토큰(사용자 차례의 끝 표시 2개, 어시스턴트 차례의 시작 표시 3개)" in d["precise"]
    assert d["precise"].endswith("prompt_eval_cached_count는 Ollama가 준 값을 그대로 보여 줍니다.")
    assert len(d["unverified"]) == 2                                   # 이전 지시의 두 항목은 이 문구로 대체됨
    assert d["unverified"][0].startswith(
        "prompt_eval_cached_count의 정확한 의미는 확인하지 않았습니다. 관측값은 0, 24, 30, 47, 71입니다.")
    assert d["unverified"][1] == "Ollama가 실제로 붙인 틀의 내용은 context의 토큰 번호로 직접 확인하지 않았습니다."


def test_wording_changed_by_the_review_is_exact():
    assert load("restore")["failures"] == [
        "스트림이 중간에 끊기면 이 단계는 건너뜁니다(skipped). 그때까지 받은 조각은 쓰기(decode) 단계에 남아 있습니다."]
    assert load("tokenize")["unverified"][1] == (
        "덩어리 가르기 규칙은 tokenizer.json의 Split 정규식입니다. 이 규칙이 각 글자를 왜 그렇게 가르는지"
        "(예: 숫자를 한 자리씩 자르는 이유)를 정규식 한 줄씩 해석해 확인하지는 않았습니다. "
        "정규식 원문은 토큰화 5단계 화면(예정)에서 보여 줍니다.")
    assert load("load")["failures"] == [
        "모델이 내려가 있으면 첫 호출에서 수 초에서 수십 초가 걸려, "
        "이 프로젝트가 AI 호출에 두려는 시간 제한(설계값 30초)을 넘길 수 있습니다."]
    assert load("load")["unverified"] == [
        "설치 직후 첫 호출(2026-10-27)에서는 올리기 19.84초와 읽기 15.67초가 모두 길었지만, "
        "이후 콜드 호출의 올리기는 약 2.7초였습니다. "
        "처음에만 길었던 원인(디스크 첫 읽기, GPU 첫 준비 등)은 확인하지 못했습니다."]
    assert len(load("tokenize")["unverified"]) == 2 and len(load("restore")["failures"]) == 1


def test_wording_changed_after_checking_the_records_is_exact():
    assert load("decode")["unverified"] == [
        "조각 수와 eval_count가 다른 이유는 확인하지 못했습니다. "
        "관측: 길이 한도에 닿은 실행(length)은 같거나(6/6, 512/512) 1 적었고(19/20), "
        "모델이 스스로 끝낸 실행(stop)은 1~3 적었습니다(34/35는 1, 38/40과 36/38은 2, 35/38은 3). "
        "가설: 차이 = (stop일 때 끝 표시 토큰 1) + "
        "(글자가 덜 끝난 바이트 조각 토큰이 다음 토큰과 합쳐져 한 조각으로 도착한 횟수). "
        "이 식이 지금까지의 관측을 모두 설명하지만, "
        "토큰별 바이트를 보여 주는 logprobs 화면으로 확인할 예정입니다."]
    assert load("prefill")["unverified"][0] == (
        "prompt_eval_cached_count의 정확한 의미는 확인하지 않았습니다. 관측값은 0, 24, 30, 47, 71입니다. "
        "직전 요청과 앞부분이 같은 만큼을 재사용한 값이라는 가설이 다섯 값을 모두 설명합니다"
        "(모델이 새로 올라간 직후 0, 같은 앞 틀만 같으면 24, "
        "같은 입력을 연달아 보내면 마지막 1토큰을 뺀 전부(31토큰 입력에서 30, 72토큰 입력에서 71), "
        "앞 24토큰 + 사용자 글 23토큰이 같고 그다음이 갈라지면 47). 이를 확인하는 실험은 아직 하지 않았습니다.")


def test_settings_help_is_the_approved_wording():
    assert json.loads((CONTENT / "settings.json").read_text(encoding="utf-8")) == OPT_HELP
