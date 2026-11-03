"""A1-T5b: 엄격 채점, 키워드 v2, 다시 채점(--rescore), 시나리오 s6·s7, warm 설정 시험.

T5a 시험(test_eval.py)의 도우미(가짜 chat 결과, 정답 답)를 그대로 쓴다. T5a 시험 파일은 수정하지 않는다.
v1 동작이 그대로인지(기본값, 키워드 v1, 시나리오 5개, 설정 8개)도 여기서 확인한다.
Ollama는 부르지 않는다.
"""
import inspect
import json
from datetime import UTC, datetime

import pytest
import test_eval as t5a

scoring, run_eval = t5a.scoring, t5a.run_eval
BOTH = [run_eval.SCENARIO_DIR, run_eval.EXTRA_SCENARIO_DIR]
NOW = datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC)

GOOD_ALL = {
    **t5a.GOOD,
    "s6_new_path_error": {
        "summary": "/checkout 경로에서 ValueError 예외로 5xx가 200건 났다.",
        "causes": ["/checkout 경로의 앱 예외다.", "입력값 변환(ValueError)에서 오류가 난다."],
        "checks": ["docker compose logs app에서 /checkout 로그를 본다.", "경로별 5xx를 본다."]},
    "s7_db_slow_combo": {
        "summary": "DB 연결 실패(OperationalError)로 5xx와 지연이 함께 났다.",
        "causes": ["db에 연결하지 못해 /가 실패하고 연결 대기로 느려진다.", "OperationalError가 반복된다."],
        "checks": ["docker compose ps로 db 상태를 본다.", "docker compose logs db를 본다."]},
}
ALL_IDS = [*t5a.IDS, "s6_new_path_error", "s7_db_slow_combo"]


def _sid(user: str) -> str:
    if "ValueError" in user:
        return "s6_new_path_error"
    if "R2(" in user and "OperationalError" in user:
        return "s7_db_slow_combo"
    return next(i for i, key in (("s1_error_spike", "RuntimeError"), ("s2_db_down", "OperationalError"),
                                 ("s3_latency", "/slow"), ("s4_app_down", "gaierror"),
                                 ("s5_weak_signal", "rpm 120")) if key in user)


class Chat:
    """llm_client.chat과 같은 인자를 받는 가짜. answer_for(시나리오 id, 호출 번호) → LlmResult."""

    def __init__(self, answer_for=None):
        self.calls: list = []
        self.answer_for = answer_for

    def __call__(self, url, model, messages, options, fmt=None, keep_alive="24h", connect_timeout=5.0,
                 total_timeout=30.0):
        n = len(self.calls)
        self.calls.append({"messages": messages, "options": options})
        sid = _sid(messages[1]["content"])
        return self.answer_for(sid, n) if self.answer_for else t5a._result(GOOD_ALL[sid])


def _scenarios(only=None, keywords="v1"):
    return run_eval.load_scenarios(BOTH, only, keywords)


def _run(only, chat, settings=run_eval.SETTINGS, keywords="v1"):
    return run_eval.run_all(
        _scenarios(only, keywords), t5a.analysis.load_runbooks(t5a.RUNBOOK_DIR), chat=chat,
        url="http://127.0.0.1:1", model="m", cfg=t5a.config.Config(), keep_alive="24h", connect_timeout=5.0,
        total_timeout=30.0, settings=settings)


def _score(sid, answer, keywords="v1", runbooks_text=""):
    sc = _scenarios([sid], keywords)[0]
    _, messages, _ = run_eval.build_prompt(sc, runbooks_text)
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    return scoring.score_answer(sc, t5a.analysis.parse_answer(text), messages[1]["content"], runbooks_text)


def _boom(*a, **k):
    raise AssertionError("Ollama를 부르면 안 된다")


# ---------------------------------------------------------------- 숫자 경계 비교

def test_invented_numbers_use_digit_boundaries():
    f = scoring.invented_numbers
    assert f(["20건"], "5xx 478/520") == ["20"]              # 520 안의 20은 일치가 아니다
    assert f(["20건"], "요청 20건") == []
    assert f(["0.92"], "에러율 10.92") == ["0.92"]
    assert f(["0.92"], "에러율 0.92, 5xx") == []
    assert f(["9"], "점수 99.9") == ["9"]                    # 99.9의 어느 9도 독립된 숫자가 아니다
    assert f(["478"], "5xx가 478.") == []                    # 문장 끝의 점은 소수점이 아니다
    assert f(["478"], "478.5건") == ["478"]
    assert f(["5"], "5xx") == []
    assert f(["1 2 3"], "") == []                            # 1~3은 목록 번호라 뺀다


# ---------------------------------------------------------------- 엄격 채점

@pytest.mark.parametrize("sid", ALL_IDS)
def test_good_answers_are_strict_passed(sid):
    s = _score(sid, GOOD_ALL[sid])
    assert s["passed"] is True and s["strict_passed"] is True and s["unsupported_causes"] == 0
    assert s["cause_matches"] == len(GOOD_ALL[sid]["causes"]) or sid == "s5_weak_signal"


def test_unsupported_cause_blocks_strict_but_not_passed():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["/error 경로가 RuntimeError 예외를 낸다.", "서버 설정이 이상하다."])
    s = _score("s1_error_spike", ans)
    assert (s["cause_matches"], s["unsupported_causes"]) == (1, 1)
    assert s["passed"] is True and s["strict_passed"] is False


def test_cause_with_unknown_marker_counts_as_supported():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["/error 예외다.", "나머지 원인은 알 수 없음이다."])
    s = _score("s1_error_spike", ans)
    assert (s["cause_matches"], s["unsupported_causes"], s["strict_passed"]) == (1, 0, True)


def test_failed_parse_has_zero_cause_counts_and_not_strict():
    s = _score("s1_error_spike", "JSON이 아니다")
    assert (s["cause_matches"], s["unsupported_causes"], s["strict_passed"]) == (0, 0, False)
    assert scoring.score_answer(_scenarios(["s1"])[0], None, "", "")["strict_passed"] is False


def test_strict_catches_a_wrong_cause_that_v1_lets_through():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["DB 연결 문제로 5xx가 날 수 있다.", "/error 예외다."])
    s = _score("s1_error_spike", ans)
    # v1은 원인 하나만 맞으면 통과(느슨함), strict가 잡는다
    assert s["passed"] is True and s["strict_passed"] is False


# ---------------------------------------------------------------- 키워드 v2

def test_with_keywords_v1_is_identity_and_v2_merges_expect_v2():
    scs = {s["id"]: s for s in _scenarios()}
    s1, s5 = scs["s1_error_spike"], scs["s5_weak_signal"]
    assert scoring.with_keywords(s1, "v1") is s1
    v2 = scoring.with_keywords(s1, "v2")
    assert v2["expect"]["forbidden_keywords"] == s1["expect"]["forbidden_keywords"] + [
        "DB 연결", "데이터베이스 연결", "DB 장애", "데이터베이스 장애"]
    assert "DB 연결" not in s1["expect"]["forbidden_keywords"]       # 원본은 바뀌지 않는다
    assert v2["expect"]["cause_keywords"] == s1["expect"]["cause_keywords"]
    v2 = scoring.with_keywords(s5, "v2")
    assert v2["expect"]["cause_keywords"] == [k for k in s5["expect"]["cause_keywords"] if k != "요청"]
    assert v2["expect"]["check_keywords"] == s5["expect"]["check_keywords"] + ["logs"]
    assert "요청" in s5["expect"]["cause_keywords"] and v2["expect"]["expect_unknown"] is True
    v2 = scoring.with_keywords(scs["s3_latency"], "v2")          # T5b 추가 지시: s3는 증상 단어·일반 단어를 뺀다
    assert v2["expect"]["cause_keywords"] == ["/slow", "느린 요청", "워커", "포화"]
    assert v2["expect"]["check_keywords"] == ["/slow", "docker compose logs app", "latency_ms", "워커"]
    assert v2["expect"]["forbidden_keywords"] == scs["s3_latency"]["expect"]["forbidden_keywords"]
    assert scs["s3_latency"]["expect"]["cause_keywords"][1:3] == ["지연", "느린"]       # v1은 그대로
    v2 = scoring.with_keywords(scs["s5_weak_signal"], "v2")
    assert v2["expect"]["forbidden_keywords"] == scs["s5_weak_signal"]["expect"]["forbidden_keywords"] + [
        "유지보수", "점검", "장애 조치"]
    for sid in ("s2_db_down", "s4_app_down", "s6_new_path_error", "s7_db_slow_combo"):
        assert scoring.with_keywords(scs[sid], "v2")["expect"] == scs[sid]["expect"]
    with pytest.raises(ValueError):
        scoring.with_keywords(s1, "v3")


def test_load_scenarios_applies_keyword_version():
    v1 = {s["id"]: s for s in _scenarios()}
    v2 = {s["id"]: s for s in _scenarios(keywords="v2")}
    assert v1["s1_error_spike"]["expect"] != v2["s1_error_spike"]["expect"]
    assert v1["s2_db_down"]["expect"] == v2["s2_db_down"]["expect"]


def test_v2_catches_db_cause_on_s1_and_loose_request_keyword_on_s5():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["/error 예외다.", "DB 연결 문제로 5xx가 날 수 있다."])
    assert _score("s1_error_spike", ans, "v1")["no_hallucination"] is True
    s = _score("s1_error_spike", ans, "v2")
    assert s["forbidden_hits"] == ["DB 연결"] and s["passed"] is False
    ans = {"summary": "요청이 줄었다.", "causes": ["요청 수가 줄었다.", "서버가 바쁘다."],
           "checks": ["요청 로그를 본다.", "기다린다."]}
    assert _score("s5_weak_signal", ans, "v1")["unknown_ok"] is True        # v1: "요청"만으로 통과
    assert _score("s5_weak_signal", ans, "v2")["unknown_ok"] is False
    ans = dict(t5a.GOOD["s5_weak_signal"], checks=["docker compose logs app를 본다.", "기다린다."])
    assert _score("s5_weak_signal", ans, "v1")["check_ok"] is False
    assert _score("s5_weak_signal", ans, "v2")["check_ok"] is True           # "logs" 추가


def test_cli_rejects_unknown_keyword_version():
    with pytest.raises(SystemExit):
        run_eval.main(["--keywords", "v3"], out=lambda s: None)


# ---------------------------------------------------------------- v1 동작이 그대로임

def test_v1_defaults_are_unchanged():
    assert [s["id"] for s in run_eval.load_scenarios(run_eval.SCENARIO_DIR)] == t5a.IDS
    assert len(run_eval.SETTINGS) == 8 and [s for s, _, _ in run_eval.SETTINGS].count("warm") == 0
    params = inspect.signature(run_eval.main).parameters
    assert params["settings"].default is run_eval.SETTINGS and params["scenario_dirs"].default == (
        run_eval.SCENARIO_DIR,)
    s1 = run_eval.load_scenarios(run_eval.SCENARIO_DIR)[0]["expect"]
    assert s1["forbidden_keywords"] == ["메모리", "CPU", "디스크", "DDoS", "공격", "인증서", "네트워크 장비", "배포"]


def test_v1_pass_rule_is_unchanged_by_strict_fields():
    ans = dict(t5a.GOOD["s2_db_down"], causes=["db 연결이 안 된다.", "서버가 바쁘다."])
    s = _score("s2_db_down", ans)
    assert s["passed"] is True and s["strict_passed"] is False and s["unsupported_causes"] == 1


# ---------------------------------------------------------------- 새 시나리오

def test_new_scenarios_load_with_expected_shape():
    scs = {s["id"]: s for s in _scenarios()}
    assert sorted(scs) == sorted(ALL_IDS)
    s6, s7 = scs["s6_new_path_error"], scs["s7_db_slow_combo"]
    for s in (s6, s7):
        assert s["baseline_rpm"] == 300 and s["probe"] == {"consecutive_failures": 0, "detail": "HTTP 200"}
        assert s["incident"]["id"] == 1 and s["incident"]["opened_min_ago"] == 2
        assert s["expect"]["expect_unknown"] is False and "expect_v2" not in s
        assert s["records_available"] is True
    assert (s6["incident"]["rules"], s6["incident"]["severity"]) == (["R1"], "critical")
    assert (s7["incident"]["rules"], s7["current_rules"]) == (["R1", "R2"], ["R1", "R2"])
    assert "/error" in s6["expect"]["forbidden_keywords"] and "/slow" in s7["expect"]["forbidden_keywords"]


def test_new_scenario_window_stats_and_prompts():
    _, m6, w6 = run_eval.build_prompt(_scenarios(["s6"])[0], "")
    assert (w6.requests, w6.errors_5xx, w6.p95_ms) == (350, 200, 40)
    assert "ValueError 200건" in m6[1]["content"] and "/checkout 200건(5xx 200)" in m6[1]["content"]
    assert "invalid literal" not in m6[1]["content"]                  # 오류 메시지 원문은 근거에 들어가지 않는다
    _, m7, w7 = run_eval.build_prompt(_scenarios(["s7"])[0], "")
    assert (w7.requests, w7.errors_5xx, w7.p95_ms) == (230, 200, 3000)
    user = m7[1]["content"]
    assert "R1(error_rate 0.87, 5xx 200/230)" in user and "R2(p95 3000ms)" in user


def test_all_seven_scenarios_within_token_budget_with_same_system():
    rb = t5a.analysis.load_runbooks(t5a.RUNBOOK_DIR)
    systems = set()
    for sc in _scenarios():
        for text in ("", rb):
            _, messages, _ = run_eval.build_prompt(sc, text)
            assert run_eval.estimate_prompt_tokens(messages) <= t5a.analysis.PROMPT_TOKEN_BUDGET
            if text:
                systems.add(messages[0]["content"])
    assert len(systems) == 1                                         # 런북 조건의 system 글은 시나리오와 무관하게 같다


def test_dry_run_prints_new_scenarios():
    lines: list = []
    assert run_eval.main(["--dry", "--runbooks", str(t5a.RUNBOOK_DIR)], chat=_boom, out=lines.append,
                         scenario_dirs=BOTH) == 0
    text = "\n".join(lines)
    assert "===== s6_new_path_error / runbook =====" in text and "===== s7_db_slow_combo / no_runbook =====" in text


# ---------------------------------------------------------------- warm 설정

def test_settings_v2_layout():
    layout = [(s, r) for s, r, _ in run_eval.SETTINGS_V2]
    assert layout == [("prod", 1), ("prod", 2), ("prod", 3), ("warmup", 1), ("warm", 1), ("warm", 2), ("warm", 3),
                      *(("var", i) for i in range(1, 6))]
    assert len(run_eval.SETTINGS_V2) == 12
    assert all(o == {} for s, _, o in run_eval.SETTINGS_V2 if s in ("prod", "warmup", "warm"))
    assert [o for s, _, o in run_eval.SETTINGS_V2 if s == "var"] == [
        {"temperature": 0.2, "seed": i} for i in range(1, 6)]


def test_run_all_with_warm_settings_keeps_prod_options_for_warm():
    chat = Chat()
    records, aborted = _run(["s1"], chat, run_eval.SETTINGS_V2)
    assert aborted is False and len(records) == 24
    first = records[:12]
    assert [(r["setting"], r["rep"]) for r in first] == [(s, r) for s, r, _ in run_eval.SETTINGS_V2]
    prod = first[0]["options"]
    assert all(r["options"] == prod for r in first if r["setting"] in ("prod", "warmup", "warm"))
    assert prod["temperature"] == 0 and prod["seed"] == 42
    assert {r["condition"] for r in records[12:]} == {"runbook"}
    assert [c["messages"] for c in chat.calls[:4]] == [chat.calls[0]["messages"]] * 4     # 같은 요청 글


def test_warmup_is_recorded_but_excluded_from_tables():
    def answer_for(sid, n):                                          # 호출 번호 n%12==3이 warmup
        if n % 12 == 3:
            return t5a._result(dict(GOOD_ALL[sid], causes=["메모리 부족이다.", "CPU 문제다."]))
        return t5a._result(GOOD_ALL[sid])
    records, _ = _run(["s1"], Chat(answer_for), run_eval.SETTINGS_V2)
    assert [r["score"]["passed"] for r in records if r["setting"] == "warmup"] == [False, False]
    md = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"]))
    assert "| s1_error_spike | 3/3 | 3/3 | 5/5 | 3/3 | 3/3 | 5/5 |" in md
    assert "| 합계 | 3/3 | 3/3 | 5/5 | 3/3 | 3/3 | 5/5 |" in md
    assert "warmup(" not in md and "warm(3회)" in md


def test_reproducibility_table_shows_prod_and_warm_side_by_side():
    def answer_for(sid, n):                                          # warm 3번째(n%12==6)만 글이 다르다
        ans = dict(GOOD_ALL[sid], summary=GOOD_ALL[sid]["summary"] + " (다름)") if n % 12 == 6 else GOOD_ALL[sid]
        return t5a._result(ans)
    records, _ = _run(["s1"], Chat(answer_for), run_eval.SETTINGS_V2)
    md = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"]))
    sect3 = md.split("## ③")[1].split("## ④")[0]
    assert "| 조건 | 같은 시나리오 수 | 같았던 시나리오 | warm 같은 시나리오 수 | warm 같았던 시나리오 |" in sect3
    assert "| no_runbook | 1/1 | s1_error_spike | 0/1 | - |" in sect3
    assert "| runbook | 1/1 | s1_error_spike | 0/1 | - |" in sect3


def test_time_table_has_cache_average_per_setting():
    def answer_for(sid, n):
        return t5a._result(GOOD_ALL[sid], prompt_eval_cached_count=n)
    records, _ = _run(["s1"], Chat(answer_for), run_eval.SETTINGS_V2)
    sect5 = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"])).split("## ⑤")[1].split("## ⑥")[0]
    assert "설정별 캐시 재사용 평균" in sect5
    assert "prod 1 / warmup 3 / warm 5 / var 9" in sect5             # 호출 0~11의 설정별 평균
    assert "prod 13 / warmup 15 / warm 17 / var 21" in sect5         # 호출 12~23


def test_strict_table_and_sheet_line_are_present():
    def answer_for(sid, n):
        if n == 0:
            return t5a._result(dict(GOOD_ALL[sid], causes=["/error 예외다.", "서버 설정이 이상하다."]))
        return t5a._result(GOOD_ALL[sid])
    records, _ = _run(["s1"], Chat(answer_for))
    md = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"]))
    assert "## ①-2 엄격 통과율" in md and "| s1_error_spike | 2/3 | 5/5 | 3/3 | 5/5 |" in md
    assert "| s1_error_spike | 3/3 | 5/5 | 3/3 | 5/5 |" in md          # ①의 통과율은 v1 규칙 그대로
    assert "(엄격 탈락, 근거 없는 원인 1개)" in md and "| 엄격 통과 |" in md


def test_main_runs_168_calls_with_both_scenario_dirs_and_v2(tmp_path):
    chat = Chat()
    lines: list = []
    code = run_eval.main(["--out", str(tmp_path), "--runbooks", str(t5a.RUNBOOK_DIR), "--keywords", "v2"],
                         chat=chat, version=lambda url: "0.40.2", out=lines.append, now=NOW,
                         settings=run_eval.SETTINGS_V2, scenario_dirs=BOTH)
    assert code == 0 and len(chat.calls) == 168
    rows = [json.loads(x) for x in (tmp_path / "raw" / "eval-20261103T050000Z.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 168 and sum(1 for r in rows if r["setting"] == "warmup") == 14
    assert any("합계 168번 호출, 통과 154(warmup 14번은 집계에서 뺌)" in x for x in lines)
    board = (tmp_path / "scoreboard-20261103T050000Z.md").read_text(encoding="utf-8")
    assert "| 채점 키워드 버전 | v2 |" in board and "s7_db_slow_combo" in board


def test_five_consecutive_failures_still_abort_with_warm_settings():
    chat = Chat(lambda sid, n: t5a._result(ok=False, reason="timeout", text=""))
    records, aborted = _run(None, chat, run_eval.SETTINGS_V2)
    assert aborted is True and len(records) == 5


# ---------------------------------------------------------------- 다시 채점

def _write_raw(tmp_path, answer_for=None, only=("s1",)):
    chat = Chat(answer_for)
    run_eval.main(["--out", str(tmp_path / "src"), "--runbooks", str(t5a.RUNBOOK_DIR), "--only", ",".join(only)],
                  chat=chat, version=lambda url: "0.40.2", out=lambda s: None, now=NOW, scenario_dirs=BOTH)
    return tmp_path / "src" / "raw" / "eval-20261103T050000Z.jsonl"


def _rescore(raw, tmp_path, keywords, lines, now=datetime(2026, 11, 3, 6, 0, 0, tzinfo=UTC)):
    return run_eval.main(["--rescore", str(raw), "--keywords", keywords, "--out", str(tmp_path / "re"),
                          "--runbooks", str(t5a.RUNBOOK_DIR)], chat=_boom, version=_boom, out=lines.append,
                         now=now, scenario_dirs=BOTH)


def test_rescore_v1_reproduces_recorded_pass_and_never_calls_ollama(tmp_path):
    raw = _write_raw(tmp_path)
    lines: list = []
    assert _rescore(raw, tmp_path, "v1", lines) == 0
    board = tmp_path / "re" / "scoreboard-rescore-20261103T060000Z.md"
    md = board.read_text(encoding="utf-8")
    assert not (tmp_path / "re" / "raw").exists()                    # raw는 새로 만들지 않는다
    assert "# 장애 분석 재채점표" in md and "## ①-2 엄격 통과율" in md and "## ⑦ 원본 기록 대비 변화" in md
    assert "통과 판정이 달라진 호출 0번" in md and "| 재채점 원본 |" in md
    assert any("재채점 16행(키워드 v1): 통과 16, 엄격 통과 16" in x for x in lines)


def test_rescore_reports_mismatch_with_exit_code_1_for_v1(tmp_path):
    raw = _write_raw(tmp_path)
    rows = [json.loads(x) for x in raw.read_text("utf-8").splitlines()]
    rows[2]["score"]["passed"] = False                               # 기록이 지금 규칙과 다르다고 꾸민다
    raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    lines: list = []
    assert _rescore(raw, tmp_path, "v1", lines) == 1
    assert any("불일치" in x for x in lines)
    assert any("달라진 호출 1" in x for x in lines)


def test_rescore_v2_lists_changed_calls_and_exits_zero(tmp_path):
    def answer_for(sid, n):
        if n == 0:
            return t5a._result(dict(GOOD_ALL[sid], causes=["/error 예외다.", "DB 연결 문제로 5xx가 날 수 있다."]))
        return t5a._result(GOOD_ALL[sid])
    raw = _write_raw(tmp_path, answer_for)
    lines: list = []
    assert _rescore(raw, tmp_path, "v2", lines) == 0
    md = (tmp_path / "re" / "scoreboard-rescore-20261103T060000Z.md").read_text(encoding="utf-8")
    sect7 = md.split("## ⑦")[1]
    assert "통과 판정이 달라진 호출 1번" in sect7
    assert "| s1_error_spike | no_runbook | prod1 | True | False | DB 연결 |" in sect7
    assert any("재채점 16행(키워드 v2): 통과 15" in x for x in lines)


def test_rescore_keeps_failed_calls_failed(tmp_path):
    def answer_for(sid, n):
        return t5a._result(ok=False, reason="timeout", text="") if n == 1 else t5a._result(GOOD_ALL[sid])
    raw = _write_raw(tmp_path, answer_for)
    lines: list = []
    assert _rescore(raw, tmp_path, "v1", lines) == 0
    assert any("통과 15" in x for x in lines)


def test_rescore_unknown_scenario_in_raw_is_an_error(tmp_path):
    raw = _write_raw(tmp_path)
    rows = [json.loads(x) for x in raw.read_text("utf-8").splitlines()]
    rows[0]["scenario"] = "s9_nope"
    raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    lines: list = []
    assert _rescore(raw, tmp_path, "v1", lines) == 2
    assert any("s9_nope" in x for x in lines)


def test_rescore_recomputes_numbers_with_boundaries(tmp_path):
    def answer_for(sid, n):                                          # 근거의 520 안에만 있는 20을 지어냄
        return t5a._result(dict(GOOD_ALL[sid], summary="/error 예외가 20건 났다."))
    raw = _write_raw(tmp_path, answer_for)
    rows = [json.loads(x) for x in raw.read_text("utf-8").splitlines()]
    for r in rows:                                                   # T5a 규칙(부분 문자열)으로 기록된 원본을 흉내 낸다
        r["score"]["invented_numbers"] = []
    raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    lines: list = []
    assert _rescore(raw, tmp_path, "v1", lines) == 0
    md = (tmp_path / "re" / "scoreboard-rescore-20261103T060000Z.md").read_text(encoding="utf-8")
    assert "| s1_error_spike | no_runbook | - | 20 | 8 |" in md.split("## ④")[1].split("## ⑤")[0]


# ---------------------------------------------------------------- T5b 추가: s3·s5 v2 키워드, 규칙 되풀이 원인

def test_v2_s3_rejects_symptom_words_that_v1_accepts():
    ans = {"summary": "응답이 느리다.", "causes": ["응답 지연이 발생했다.", "대기 시간이 길다."],
           "checks": ["latency를 확인한다.", "app 상태를 본다."]}
    v1 = _score("s3_latency", ans, "v1")
    assert v1["cause_ok"] is True and v1["check_ok"] is True and v1["passed"] is True       # 엉뚱한 답도 v1은 통과
    v2 = _score("s3_latency", ans, "v2")
    assert v2["cause_ok"] is False and v2["check_ok"] is False and v2["passed"] is False
    assert _score("s3_latency", t5a.GOOD["s3_latency"], "v2")["strict_passed"] is True


def test_v2_s5_forbids_maintenance_words():
    ans = dict(t5a.GOOD["s5_weak_signal"], causes=["알 수 없음", "서버 점검 중이라 요청이 줄었을 수 있다."])
    assert _score("s5_weak_signal", ans, "v1")["no_hallucination"] is True
    s = _score("s5_weak_signal", ans, "v2")
    assert s["forbidden_hits"] == ["점검"] and s["passed"] is False
    for word in ("유지보수", "장애 조치"):
        ans = dict(t5a.GOOD["s5_weak_signal"], summary=f"{word}가 원인일 수 있다.")
        assert word in _score("s5_weak_signal", ans, "v2")["forbidden_hits"]


def test_rule_echo_causes_counts_rule_names_and_stays_out_of_the_verdict():
    base = t5a.GOOD["s1_error_spike"]
    s = _score("s1_error_spike", dict(base, causes=["R1 규칙이 켜졌다.", "/error 예외다."]))
    assert s["rule_echo_causes"] == 1
    assert _score("s1_error_spike", base)["rule_echo_causes"] == 0
    assert _score("s1_error_spike", dict(base, causes=["R5가 켜졌다.", "/error 예외다."]))["rule_echo_causes"] == 0
    assert _score("s1_error_spike", dict(base, causes=["R1과 R2가 켜졌다.", "R4도 켜졌다."]))["rule_echo_causes"] == 2
    ok = _score("s1_error_spike", dict(base, causes=["R1이 켜졌고 /error 예외다.", "앱 내부 오류다."]))
    assert ok["rule_echo_causes"] == 1 and ok["unsupported_causes"] == 0 and ok["strict_passed"] is True
    assert _score("s1_error_spike", "JSON이 아니다")["rule_echo_causes"] == 0


def test_table_2_shows_rule_echo_row():
    def answer_for(sid, n):
        if n == 0:
            return t5a._result(dict(GOOD_ALL[sid], causes=["R1이 켜졌고 /error 예외다.", "앱 내부 오류다."]))
        return t5a._result(GOOD_ALL[sid])
    records, _ = _run(["s1"], Chat(answer_for))
    md = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"]))
    sect2 = md.split("## ②")[1].split("## ③")[0]
    assert "| 규칙 이름 되풀이 원인(R1~R4, 보고용: 호출 수/원인 수) | 1/1 | 0/0 | 0/0 | 0/0 |" in sect2


# ---------------------------------------------------------------- T5c: strict_v3

@pytest.mark.parametrize("sid", ALL_IDS)
def test_good_answers_are_strict_v3_passed(sid):
    assert _score(sid, GOOD_ALL[sid])["strict_v3_passed"] is True


def test_strict_v3_blocks_rule_echo_cause_that_strict_lets_through():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["/error 예외다.", "R1이 켜졌으므로 에러율이 높다(에러 /error)."])
    s = _score("s1_error_spike", ans)
    assert s["rule_echo_causes"] == 1
    assert s["strict_passed"] is True and s["strict_v3_passed"] is False


def test_strict_v3_requires_unknown_mark_for_expect_unknown_scenario():
    sid = "s5_weak_signal"
    ans = dict(GOOD_ALL[sid], summary="요청이 줄었다.", causes=["트래픽이 감소했다.", "클라이언트 유입이 줄었다."],
               checks=["트래픽 추세를 본다.", "요청 로그를 본다."])
    s = _score(sid, ans)
    assert s["strict_passed"] is True and s["strict_v3_passed"] is False
    s = _score(sid, dict(ans, checks=["트래픽 추세를 본다.", "원인은 알 수 없음으로 남긴다."]))
    assert s["strict_v3_passed"] is True


def test_strict_v3_is_false_when_strict_is_false_and_for_failed_parse():
    ans = dict(t5a.GOOD["s1_error_spike"], causes=["/error 경로가 RuntimeError 예외를 낸다.", "서버 설정이 이상하다."])
    assert _score("s1_error_spike", ans)["strict_v3_passed"] is False
    assert _score("s1_error_spike", "JSON이 아니다")["strict_v3_passed"] is False


def test_strict_v3_table_rows_are_present():
    records, _ = _run(["s1"], Chat())
    md = run_eval.render_scoreboard(t5a.META, records, _scenarios(["s1"]))
    assert "## ①-3 엄격 v3 통과율" in md
    assert "| 엄격 v3 통과 |" in md.split("## ②")[1].split("## ③")[0]
