"""A1-T5a: scripts/eval의 채점 함수(scoring)와 실행기(run_eval) 시험.

Ollama는 부르지 않는다. llm_client.LlmResult를 돌려주는 가짜 chat 함수를 주입해
합성 답으로 채점·표 렌더링·실행 순서·중단을 본다. 시나리오 5개가 운영의 build_evidence·build_messages를
통과하는지, 프롬프트가 토큰 예산 안인지도 확인한다.
"""
import atexit
import importlib
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import analysis
import config
import llm_client

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "runbooks"


def _load_eval() -> Path:
    """scripts/eval(py 파일과 scenarios)을 임시 폴더의 레포 모양 경로(.../scripts/eval)로 복사해 import 경로에 둔다.
    check-watcher.sh 컨테이너는 이 폴더를 /eval에 마운트하므로 레포 경로와 /eval 둘 다 찾는다."""
    here = Path(__file__).resolve()
    src = next((c for c in (here.parents[2] / "scripts" / "eval", Path("/eval")) if (c / "run_eval.py").exists()), None)
    if src is None:
        raise RuntimeError("scripts/eval을 찾지 못했다")
    root = Path(tempfile.mkdtemp(prefix="eval-test-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    dst = root / "repo" / "scripts" / "eval"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


sys.path.insert(0, str(_load_eval()))
scoring = importlib.import_module("scoring")
run_eval = importlib.import_module("run_eval")

SCENARIO_DIR = Path(run_eval.__file__).resolve().parent / "scenarios"
OPTION_KEYS = {"temperature", "seed", "top_k", "top_p", "repeat_penalty", "num_predict", "num_ctx"}
IDS = ["s1_error_spike", "s2_db_down", "s3_latency", "s4_app_down", "s5_weak_signal"]

# 시나리오별 정답 답(합성). 키워드·금지어 기준으로 통과해야 한다.
GOOD = {
    "s1_error_spike": {
        "summary": "/error 경로에서 RuntimeError 예외로 5xx가 478건 났다.",
        "causes": ["/error 경로가 RuntimeError 예외를 낸다.", "앱 내부 오류가 한 경로에 몰렸다."],
        "checks": ["docker compose logs app에서 /error 로그를 본다.", "경로별 5xx 비율을 본다."]},
    "s2_db_down": {
        "summary": "DB 연결 실패로 /가 OperationalError 오류를 낸다.",
        "causes": ["db 컨테이너에 연결하지 못한다.", "OperationalError가 /에서 반복된다."],
        "checks": ["docker compose ps로 db 상태를 본다.", "docker compose logs db를 본다."]},
    "s3_latency": {
        "summary": "/slow 요청 때문에 p95가 올랐다.",
        "causes": ["/slow가 워커를 붙잡아 다른 요청이 대기한다.", "느린 요청이 포화를 만든다."],
        "checks": ["/slow 호출량을 본다.", "docker compose logs app의 latency_ms를 본다."]},
    "s4_app_down": {
        "summary": "프로브가 연속으로 실패했다.",
        "causes": ["app 컨테이너가 정지했거나 재시작 중이다.", "프로브에 응답이 없다."],
        "checks": ["docker compose ps로 app 상태를 본다.", "docker compose logs app의 마지막 줄을 본다."]},
    "s5_weak_signal": {
        "summary": "요청 수만 줄었고 다른 신호는 정상이다.",
        "causes": ["알 수 없음: 근거가 부족하다.", "클라이언트 유입이 줄었을 수 있다."],
        "checks": ["트래픽 추세를 본다.", "요청 로그를 본다."]},
}


def _result(answer=None, *, text=None, ok=True, reason=None, **kw):
    body = text if text is not None else json.dumps(answer, ensure_ascii=False)
    base = dict(ok=ok, reason=reason, text=body if ok or text is not None else "", wall_ms=1200.0, http_status=200,
                prompt_eval_count=500, prompt_eval_cached_count=300, eval_count=80, done_reason="stop")
    base.update(kw)
    return llm_client.LlmResult(**base)


class FakeChat:
    """llm_client.chat과 같은 인자를 받는다. answer_for(scenario_id, call_no) → LlmResult 로 답을 정한다."""

    def __init__(self, answer_for=None):
        self.calls: list = []
        self.answer_for = answer_for

    def __call__(self, url, model, messages, options, fmt=None, keep_alive="24h", connect_timeout=5.0,
                 total_timeout=30.0):
        n = len(self.calls)
        self.calls.append({"url": url, "model": model, "messages": messages, "options": options, "fmt": fmt,
                           "keep_alive": keep_alive, "connect_timeout": connect_timeout,
                           "total_timeout": total_timeout})
        user = messages[1]["content"]
        sid = next((i for i, key in (("s1_error_spike", "RuntimeError"), ("s2_db_down", "OperationalError"),
                                     ("s3_latency", "/slow"), ("s4_app_down", "gaierror"),
                                     ("s5_weak_signal", "rpm 120")) if key in user), None)
        return self.answer_for(sid, n) if self.answer_for else _result(GOOD[sid])


def _run(scenarios, chat, runbooks_text=""):
    cfg = config.Config()
    return run_eval.run_all(scenarios, runbooks_text, chat=chat, url="http://127.0.0.1:1", model="m", cfg=cfg,
                            keep_alive="24h", connect_timeout=5.0, total_timeout=30.0)


def _scenario(sid):
    return next(s for s in run_eval.load_scenarios(SCENARIO_DIR) if s["id"] == sid)


def _score(sid, answer_text, runbooks_text=""):
    sc = _scenario(sid)
    _, messages, _ = run_eval.build_prompt(sc, runbooks_text)
    text = answer_text if isinstance(answer_text, str) else json.dumps(answer_text, ensure_ascii=False)
    return scoring.score_answer(sc, analysis.parse_answer(text), messages[1]["content"], runbooks_text)


# ---------------------------------------------------------------- 채점

@pytest.mark.parametrize("sid", IDS)
def test_good_answer_passes_every_scenario(sid):
    s = _score(sid, GOOD[sid])
    assert s["passed"] is True, s
    assert s["forbidden_hits"] == [] and s["invented_numbers"] == []


def test_forbidden_keyword_fails_hallucination_check():
    ans = dict(GOOD["s1_error_spike"], causes=["/error 예외다.", "메모리 부족이 원인일 수 있다."])
    s = _score("s1_error_spike", ans)
    assert s["cause_ok"] and s["check_ok"]
    assert s["forbidden_hits"] == ["메모리"]
    assert s["no_hallucination"] is False and s["passed"] is False


def test_forbidden_is_searched_in_summary_too():
    ans = dict(GOOD["s3_latency"], summary="DDoS 공격으로 보인다.")
    s = _score("s3_latency", ans)
    assert s["forbidden_hits"] == ["DDoS", "공격"] and s["passed"] is False


def test_keywords_are_case_insensitive():
    ans = dict(GOOD["s1_error_spike"], causes=["runtimeerror가 반복된다.", "기타 원인"])
    assert _score("s1_error_spike", ans)["cause_ok"] is True


def test_missing_cause_or_check_keyword_fails():
    ans = dict(GOOD["s2_db_down"], causes=["서버가 이상하다.", "원인 불명이다."])
    s = _score("s2_db_down", ans)
    assert s["cause_ok"] is False and s["check_ok"] is True and s["passed"] is False
    ans = dict(GOOD["s2_db_down"], checks=["다시 시도한다.", "기다려 본다."])
    s = _score("s2_db_down", ans)
    assert s["check_ok"] is False and s["passed"] is False


def test_invented_numbers_are_reported_but_do_not_decide_pass():
    ans = dict(GOOD["s1_error_spike"], summary="/error 예외가 478건, 평소의 45%와 99.9ms 수준이며 1, 2, 3번이다.")
    s = _score("s1_error_spike", ans)
    assert s["invented_numbers"] == ["45", "99.9"]        # 478은 근거에 있고 1~3은 목록 번호라 뺀다
    assert s["passed"] is True


def test_invented_numbers_accept_runbook_numbers():
    rb = analysis.load_runbooks(RUNBOOK_DIR)
    # 20은 근거의 520에 글자로 들어 있어 못 잡는다(부분 문자열 비교). 2000은 근거에 없고 런북에 있다.
    ans = dict(GOOD["s1_error_spike"], summary="/error 예외다. p95 기준은 2000ms다.")
    assert _score("s1_error_spike", ans, "")["invented_numbers"] == ["2000"]
    assert _score("s1_error_spike", ans, rb)["invented_numbers"] == []


def test_unknown_marker_passes_expect_unknown_scenario():
    s = _score("s5_weak_signal", GOOD["s5_weak_signal"])
    assert s["unknown_ok"] is True and s["passed"] is True


def test_unknown_ok_without_marker_needs_cause_and_no_forbidden():
    ans = {"summary": "요청이 줄었다.", "causes": ["클라이언트 요청이 감소했다.", "유입이 줄었다."],
           "checks": ["요청 로그를 본다.", "트래픽을 본다."]}
    assert _score("s5_weak_signal", ans)["unknown_ok"] is True
    ans = {"summary": "원인 불명.", "causes": ["원인 불명이다.", "확인 불가다."],
           "checks": ["요청 로그를 본다.", "기다린다."]}
    s = _score("s5_weak_signal", ans)
    assert s["unknown_ok"] is False and s["passed"] is False


def test_unknown_marker_does_not_excuse_forbidden_keyword():
    ans = dict(GOOD["s5_weak_signal"], causes=["알 수 없음", "메모리 부족일 수 있다."])
    s = _score("s5_weak_signal", ans)
    assert s["unknown_ok"] is True and s["no_hallucination"] is False and s["passed"] is False


def test_unknown_ok_is_none_for_normal_scenarios():
    assert _score("s1_error_spike", GOOD["s1_error_spike"])["unknown_ok"] is None


@pytest.mark.parametrize("text", [
    "이건 JSON이 아니다", "[1, 2]",
    json.dumps({"summary": "a", "causes": ["하나"], "checks": ["a", "b"]})])      # 원인이 1개(최소 2개)
def test_bad_format_scores_all_false(text):
    s = _score("s1_error_spike", text)
    assert s["format_ok"] is False and s["passed"] is False
    assert s["cause_ok"] is False and s["check_ok"] is False and s["no_hallucination"] is False


def test_foreign_script_fails_language():
    ans = dict(GOOD["s1_error_spike"], summary="/error 예외가 났다。请检查。")
    s = _score("s1_error_spike", ans)
    assert s["language_ok"] is False and s["passed"] is False


def test_failed_call_scores_as_failure():
    sc = _scenario("s1_error_spike")
    s = scoring.score_answer(sc, None, "", "")
    assert s["format_ok"] is False and s["passed"] is False and s["invented_numbers"] == []


# ---------------------------------------------------------------- 시나리오 로딩·레코드 펼치기

def test_load_scenarios_has_five_with_expected_shape():
    scs = run_eval.load_scenarios(SCENARIO_DIR)
    assert [s["id"] for s in scs] == IDS
    keys = {"id", "title", "ground_truth", "incident", "current_rules", "records_available", "records",
            "baseline_rpm", "probe", "expect"}
    for s in scs:
        assert keys <= set(s)
        assert set(s["expect"]) == {"cause_keywords", "check_keywords", "forbidden_keywords", "expect_unknown"}
    assert [s["id"] for s in scs if s["expect"]["expect_unknown"]] == ["s5_weak_signal"]
    assert [s["id"] for s in scs if not s["records_available"]] == ["s4_app_down"]
    assert _scenario("s4_app_down")["probe"] == {"consecutive_failures": 2, "detail": "gaierror"}


def test_load_scenarios_only_filter():
    assert [s["id"] for s in run_eval.load_scenarios(SCENARIO_DIR, ["s1", "s4_app_down"])] == [IDS[0], IDS[3]]
    with pytest.raises(ValueError):
        run_eval.load_scenarios(SCENARIO_DIR, ["s9"])


def test_expand_records_matches_log_line_shape_and_counts():
    recs = run_eval.expand_records(_scenario("s1_error_spike"))
    assert len(recs) == 520
    assert set(recs[0]) == {"ts", "service", "request_id", "method", "path", "status", "latency_ms", "error"}
    assert sum(1 for r in recs if r["path"] == "/error" and r["status"] == 500) == 478
    assert recs[0]["error"] == "RuntimeError: simulated failure (/error)" and recs[-1]["error"] is None
    assert len({r["request_id"] for r in recs}) == 520
    assert run_eval.expand_records(_scenario("s4_app_down")) == []


def test_window_stats_come_from_stats_summarize():
    _, _, w = run_eval.build_prompt(_scenario("s1_error_spike"), "")
    assert (w.requests, w.errors_5xx) == (520, 478)
    _, _, w = run_eval.build_prompt(_scenario("s2_db_down"), "")
    assert (w.requests, w.errors_5xx, w.p95_ms) == (320, 300, 3000)       # /healthz 15건은 집계에서 빠진다
    _, _, w = run_eval.build_prompt(_scenario("s3_latency"), "")
    assert (w.requests, w.errors_5xx, w.p95_ms) == (240, 0, 3200)
    _, _, w = run_eval.build_prompt(_scenario("s4_app_down"), "")
    assert (w.requests, w.errors_5xx, w.p95_ms, w.rpm) == (0, 0, 0, 0)
    _, _, w = run_eval.build_prompt(_scenario("s5_weak_signal"), "")
    assert (w.requests, w.rpm) == (120, 120)


# ---------------------------------------------------------------- 프롬프트

def test_all_scenarios_build_prompts_within_budget_with_same_system():
    rb = analysis.load_runbooks(RUNBOOK_DIR)
    assert rb
    systems = {"runbook": set(), "no_runbook": set()}
    for sc in run_eval.load_scenarios(SCENARIO_DIR):
        for cond, text in (("no_runbook", ""), ("runbook", rb)):
            evidence, messages, _ = run_eval.build_prompt(sc, text)
            assert [m["role"] for m in messages] == ["system", "user"]
            assert run_eval.estimate_prompt_tokens(messages) <= analysis.PROMPT_TOKEN_BUDGET
            assert evidence["records_available"] is sc["records_available"]
            systems[cond].add(messages[0]["content"])
    assert len(systems["runbook"]) == 1 and len(systems["no_runbook"]) == 1
    assert systems["no_runbook"] == {analysis.SYSTEM_PROMPT_V1}


def test_s4_user_text_has_no_window_numbers():
    _, messages, _ = run_eval.build_prompt(_scenario("s4_app_down"), "")
    user = messages[1]["content"]
    assert "[최근" not in user and "로그 창이 닫히기 전에 열려" in user and "gaierror" in user


# ---------------------------------------------------------------- 실행 (가짜 chat)

def test_run_all_makes_80_calls_with_seven_options_each():
    chat = FakeChat()
    records, aborted = _run(run_eval.load_scenarios(SCENARIO_DIR), chat, analysis.load_runbooks(RUNBOOK_DIR))
    assert aborted is False and len(records) == len(chat.calls) == 80
    base = config.Config()
    for rec in records:
        assert set(rec["options"]) == OPTION_KEYS
        assert rec["options"]["top_k"] == base.llm_top_k and rec["options"]["num_ctx"] == base.llm_num_ctx
    first = records[:8]                                              # 같은 시나리오·조건의 8번이 연달아 간다
    assert {(r["scenario"], r["condition"]) for r in first} == {("s1_error_spike", "no_runbook")}
    assert [(r["setting"], r["options"]["temperature"], r["options"]["seed"]) for r in first] == (
        [("prod", 0, 42)] * 3 + [("var", 0.2, s) for s in range(1, 6)])
    assert [(r["scenario"], r["condition"]) for r in records[8:16]] == [("s1_error_spike", "runbook")] * 8
    for c in chat.calls:
        assert c["fmt"] is analysis.ANALYSIS_SCHEMA and c["keep_alive"] == "24h"
        assert (c["connect_timeout"], c["total_timeout"]) == (5.0, 30.0)
    # 런북 조건에서만 system에 런북이 들어간다
    # system 지시문에도 "[런북]"이 있어 제목까지 본다
    assert "[런북] 5xx" not in chat.calls[0]["messages"][0]["content"]
    assert "[런북] 5xx" in chat.calls[8]["messages"][0]["content"]
    assert all(r["score"]["passed"] for r in records)


def test_failed_call_is_recorded_and_run_continues():
    def answer_for(sid, n):
        return _result(ok=False, reason="timeout", text="") if n == 1 else _result(GOOD[sid])
    records, aborted = _run(run_eval.load_scenarios(SCENARIO_DIR, ["s1"]), FakeChat(answer_for))
    assert aborted is False and len(records) == 16
    bad = records[1]
    assert bad["ok"] is False and bad["fail_reason"] == "timeout" and bad["parsed"] is None
    assert bad["score"]["passed"] is False
    assert records[2]["ok"] is True and records[2]["score"]["passed"] is True


def test_five_consecutive_failures_abort_the_run():
    chat = FakeChat(lambda sid, n: _result(ok=False, reason="connect_error", text=""))
    records, aborted = _run(run_eval.load_scenarios(SCENARIO_DIR), chat)
    assert aborted is True and len(records) == len(chat.calls) == run_eval.MAX_CONSECUTIVE_FAILS


def test_failure_streak_resets_after_a_success():
    def answer_for(sid, n):                                          # 4번 실패, 1번 성공을 되풀이한다
        return _result(GOOD[sid]) if n % 5 == 4 else _result(ok=False, reason="timeout", text="")
    records, aborted = _run(run_eval.load_scenarios(SCENARIO_DIR, ["s1"]), FakeChat(answer_for))
    assert aborted is False and len(records) == 16


def test_truncated_answer_counts_as_failure():
    chat = FakeChat(lambda sid, n: _result(GOOD[sid], ok=False, reason="truncated", done_reason="length"))
    records, aborted = _run(run_eval.load_scenarios(SCENARIO_DIR, ["s1"]), chat)
    assert aborted is True and records[0]["fail_reason"] == "truncated" and records[0]["parsed"] is None


# ---------------------------------------------------------------- 표 렌더링

META = {"started_utc": "20261103T000000Z", "model": "qwen2.5:7b", "ollama_version": "0.40.2",
        "prompt_version": "v1", "system_sha256": "ab" * 32,
        "options_prod": {"temperature": 0, "seed": 42}, "options_var": {"temperature": 0.2, "seed": "1~5"},
        "keep_alive": "24h", "connect_timeout": 5.0, "total_timeout": 30.0, "runbook_chars": 1000, "aborted": False}


def _board(chat, only=None, **meta):
    scs = run_eval.load_scenarios(SCENARIO_DIR, only)
    records, _ = _run(scs, chat, analysis.load_runbooks(RUNBOOK_DIR))
    return run_eval.render_scoreboard({**META, **meta}, records, scs), records


def test_scoreboard_has_all_sections_and_conditions_table():
    md, _ = _board(FakeChat())
    for head in ("## 조건", "## ① ", "## ② ", "## ③ ", "## ④ ", "## ⑤ ", "## ⑥ "):
        assert head in md
    assert md.index("## 조건") < md.index("## ① ")
    assert "합성" in md and "qwen2.5:7b" in md and "0.40.2" in md and "ab" * 32 in md and "v1" in md
    assert "| s1_error_spike | 3/3 | 5/5 | 3/3 | 5/5 |" in md
    assert "| 합계 | 15/15 | 25/25 | 15/15 | 25/25 |" in md
    assert "| 같은 시나리오 수" in md and "| no_runbook | 5/5 |" in md and "| runbook | 5/5 |" in md
    assert "정확성 0~2: ____ / 유용성 0~2: ____" in md and "### s5_weak_signal / runbook" in md
    assert "/error 경로가 RuntimeError 예외를 낸다." in md           # 사람이 볼 시트에 답 전문이 들어간다
    assert "없음" in md.split("## ④")[1].split("## ⑤")[0]


def test_scoreboard_lists_hallucinations_and_failures():
    def answer_for(sid, n):
        if n == 0:
            bad = dict(GOOD[sid], causes=["/error 예외다.", "메모리 부족일 수 있다."], summary="약 45%가 실패했다.")
            return _result(bad)
        if n == 1:
            return _result(ok=False, reason="timeout", text="")
        return _result(GOOD[sid])
    md, records = _board(FakeChat(answer_for), ["s1"])
    sect4 = md.split("## ④")[1].split("## ⑤")[0]
    assert "메모리" in sect4 and "45" in sect4
    assert "timeout" in md.split("## ⑤")[1]
    assert "| s1_error_spike | 2/3 | 5/5 | 3/3 | 5/5 |" not in md          # 첫 두 호출이 탈락이라 1/3
    assert "| s1_error_spike | 1/3 | 5/5 | 3/3 | 5/5 |" in md
    assert sum(1 for r in records if not r["ok"]) == 1


def test_reproducibility_requires_same_text_in_all_three():
    def answer_for(sid, n):
        return _result(dict(GOOD[sid], summary=f"{GOOD[sid]['summary']} ({n})")) if n == 2 else _result(GOOD[sid])
    md, _ = _board(FakeChat(answer_for), ["s1"])
    sect3 = md.split("## ③")[1].split("## ④")[0]
    assert "| no_runbook | 0/1 |" in sect3 and "| runbook | 1/1 |" in sect3


def test_scoreboard_marks_aborted_run():
    scs = run_eval.load_scenarios(SCENARIO_DIR, ["s1"])
    records, aborted = _run(scs, FakeChat(lambda sid, n: _result(ok=False, reason="connect_error", text="")))
    md = run_eval.render_scoreboard({**META, "aborted": aborted}, records, scs)
    assert "중단" in md and "connect_error" in md and "(파싱 불가 또는 호출 실패: connect_error)" in md


# ---------------------------------------------------------------- 진입점

def test_dry_run_never_calls_chat_and_prints_prompts():
    def boom(*a, **k):
        raise AssertionError("dry 모드에서 chat을 부르면 안 된다")
    lines: list = []
    code = run_eval.main(["--dry", "--runbooks", str(RUNBOOK_DIR)], chat=boom, version=boom, out=lines.append)
    text = "\n".join(lines)
    assert code == 0 and "===== s5_weak_signal / runbook =====" in text
    assert "[장애 요약]" in text and "추정 토큰" in text and "너는 웹 서비스 장애 분석 보조다" in text


def test_main_writes_raw_jsonl_and_scoreboard(tmp_path):
    chat = FakeChat()
    lines: list = []
    now = datetime(2026, 11, 3, 1, 2, 3, tzinfo=UTC)
    code = run_eval.main(["--out", str(tmp_path), "--runbooks", str(RUNBOOK_DIR), "--model", "fake:1b"],
                         chat=chat, version=lambda url: "0.40.2", out=lines.append, now=now)
    assert code == 0 and len(chat.calls) == 80
    raw = tmp_path / "raw" / "eval-20261103T010203Z.jsonl"
    board = tmp_path / "scoreboard-20261103T010203Z.md"
    rows = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 80 and board.exists()
    keys = {"scenario", "condition", "setting", "options", "text", "parsed", "score", "stats", "fail_reason"}
    assert keys <= set(rows[0])
    assert rows[0]["model"] == "fake:1b" and rows[0]["text"] and len(rows[0]["options"]) == 7
    assert "fake:1b" in board.read_text(encoding="utf-8")
    assert any("합계 80번 호출, 통과 80" in line for line in lines)


def test_main_aborts_with_exit_code_2(tmp_path):
    chat = FakeChat(lambda sid, n: _result(ok=False, reason="connect_error", text=""))
    code = run_eval.main(["--out", str(tmp_path), "--runbooks", str(RUNBOOK_DIR)], chat=chat,
                         version=lambda url: None, out=lambda s: None,
                         now=datetime(2026, 11, 3, tzinfo=UTC))
    assert code == 2 and len(chat.calls) == 5
    assert len(list((tmp_path / "raw").glob("*.jsonl"))) == 1 and len(list(tmp_path.glob("scoreboard-*.md"))) == 1


def test_main_rejects_unknown_scenario_and_bad_url(tmp_path):
    out: list = []
    assert run_eval.main(["--only", "s9", "--out", str(tmp_path)], out=out.append) == 2
    assert run_eval.main(["--url", "ftp://x", "--out", str(tmp_path)], out=out.append) == 2
