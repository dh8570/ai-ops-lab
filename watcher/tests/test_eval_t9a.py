"""A1-T9a: 실제 장애 #14의 근거 모양 시나리오(s16) 시험. 기존 시험은 수정하지 않고 test_eval의 도우미만 쓴다.
Ollama는 부르지 않는다."""
import test_eval as t5a

run_eval = t5a.run_eval
REAL_DIR = run_eval.HERE / "scenarios_real"      # 시나리오 파일 하나만 있는 새 폴더

# 실제 장애 #14에서 기록된 user 글(줄바꿈 \n, 끝에 줄바꿈 없음). 한 글자도 바꾸지 않는다.
REAL_USER = (
    "[장애 요약]\n"
    "장애 #14 · 심각도 critical · 시작 방금 · 켜진 규칙: R1(error_rate 0.38, 5xx 180/479)\n"
    "[최근 60초] 요청 479 · 5xx 180 · 에러율 0.38 · p95 27ms · 분당 요청 479\n"
    "[기준선] 평소 분당 요청 300\n"
    "[오류 유형] RuntimeError 180건\n"
    "[경로] / 299건(2xx 299) · /error 180건(5xx 180)\n"
    "[프로브] 연속 실패 0회 · 최근 결과 HTTP 200"
)


def _scenario():
    return run_eval.load_scenarios(REAL_DIR)[0]


def _messages(prompt):
    """조건 all_<prompt>의 messages: 런북은 all 모드(전체 런북), 프롬프트 버전만 다르다."""
    assert run_eval.condition_spec(f"all_{prompt}") == (prompt, "all")
    return run_eval.build_prompt(_scenario(), t5a.analysis.load_runbooks(t5a.RUNBOOK_DIR), run_eval.NOW_TS, prompt)[1]


def test_real_shape_scenario_loads():
    scs = run_eval.load_scenarios(REAL_DIR)
    assert [s["id"] for s in scs] == ["s16_real_error_shape"]
    assert scs[0]["real_shape"] is True and scs[0]["source"]
    assert scs[0]["expect"]["expect_unknown"] is False


def test_all_v1_user_message_is_identical_to_real_incident_14():
    msgs = _messages("v1")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"] == REAL_USER


def test_all_v2_user_message_is_same_and_only_system_differs():
    v1, v2 = _messages("v1"), _messages("v2")
    assert v2[1]["content"] == REAL_USER == v1[1]["content"]
    assert v2[0]["content"] != v1[0]["content"]


def test_estimated_prompt_tokens_within_3000_for_both_conditions():
    for prompt in ("v1", "v2"):
        assert run_eval.estimate_prompt_tokens(_messages(prompt)) <= 3000
