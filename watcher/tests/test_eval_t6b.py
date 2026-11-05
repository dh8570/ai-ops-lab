"""A1-T6b: 프롬프트 v3, 스키마 완화(minItems 1), 새 보류 시나리오 s12~s15, --with-holdout2, 비교표 ⑫~⑮ 시험.

기존 시험 파일은 수정하지 않고 test_eval(T5a)·test_eval_t6a(T6a)의 도우미만 가져다 쓴다. Ollama는 부르지 않는다.
"""
import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
import test_eval as t5a
import test_eval_t6a as t6a

analysis, variants, run_eval = t5a.analysis, t6a.variants, t5a.run_eval
HOLDOUT2 = run_eval.HOLDOUT2_DIR
ALL_DIRS = [run_eval.SCENARIO_DIR, run_eval.EXTRA_SCENARIO_DIR, run_eval.HOLDOUT_DIR, HOLDOUT2]
HOLDOUT2_IDS = ["s12_new_exception", "s13_latency_unlocated", "s14_probe_refused", "s15_timeout_error"]
THREE = ["all_v1", "all_v2", "all_v3"]
NOW = datetime(2026, 11, 5, 8, 0, 0, tzinfo=UTC)

V3_SHA256 = "df4f064592c82353b562c8da6e0da87b241b44084b2ced70170996d1c1f3e794"

ONE_CAUSE = {"summary": "/payment 경로의 KeyError 예외다.", "causes": ["/payment 경로에서 KeyError 예외가 난다."],
             "checks": ["docker compose logs app에서 /payment 로그를 본다."]}
TWO_CAUSE = {"summary": "/payment 예외다.", "causes": ["/payment 경로의 KeyError 예외다.", "알 수 없음: 이유"],
             "checks": ["docker compose logs app을 본다.", "/payment 경로를 본다."]}


def _all(only=None, keywords="v1"):
    return run_eval.load_scenarios(ALL_DIRS, only, keywords)


# ---------------------------------------------------------------- v3 프롬프트와 스키마

def test_system_prompt_v3_is_pinned():
    assert hashlib.sha256(variants.SYSTEM_PROMPT_V3.encode("utf-8")).hexdigest() == V3_SHA256
    assert variants.SYSTEM_PROMPT_V3 not in (analysis.SYSTEM_PROMPT_V1, variants.SYSTEM_PROMPT_V2)
    assert hashlib.sha256(variants.SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest() == t6a.V2_SHA256   # v2는 그대로


def test_conditions_include_all_v3_and_keep_the_rest():
    assert variants.CONDITIONS["all_v3"] == ("v3", "all")
    assert {k: v for k, v in variants.CONDITIONS.items() if k != "all_v3"} == {
        "nr_v1": ("v1", "none"), "all_v1": ("v1", "all"), "all_v2": ("v2", "all"),
        "match_v1": ("v1", "match"), "match_v2": ("v2", "match")}


def test_schema_v3_differs_from_v1_only_in_min_items():
    v1, v3 = analysis.ANALYSIS_SCHEMA, variants.ANALYSIS_SCHEMA_V3
    assert v3 is not v1 and v3["properties"]["causes"] is not v1["properties"]["causes"]
    for key in ("causes", "checks"):
        assert v1["properties"][key]["minItems"] == 2 and v3["properties"][key]["minItems"] == 1
        assert v3["properties"][key]["maxItems"] == 3
    fixed = copy.deepcopy(v3)
    for key in ("causes", "checks"):
        fixed["properties"][key]["minItems"] = 2
    assert fixed == v1                                      # minItems를 되돌리면 v1과 같다(원본은 바뀌지 않았다)


def test_schema_for():
    assert variants.schema_for("v1") is analysis.ANALYSIS_SCHEMA
    assert variants.schema_for("v2") is analysis.ANALYSIS_SCHEMA
    assert variants.schema_for("v3") is variants.ANALYSIS_SCHEMA_V3
    with pytest.raises(ValueError):
        variants.schema_for("v4")


# ---------------------------------------------------------------- parse_answer_variant

def test_one_cause_answer_passes_v3_schema_and_fails_v1_schema():
    text = json.dumps(ONE_CAUSE, ensure_ascii=False)
    ok = variants.parse_answer_variant(text, variants.ANALYSIS_SCHEMA_V3)
    assert ok.ok and len(ok.causes) == 1 and len(ok.checks) == 1
    bad = variants.parse_answer_variant(text, analysis.ANALYSIS_SCHEMA)
    assert (bad.ok, bad.reason) == (False, "bad_output")
    assert analysis.parse_answer(text).ok is False          # 운영 스키마는 그대로 원인 1개를 거부한다


@pytest.mark.parametrize("text", [json.dumps(TWO_CAUSE, ensure_ascii=False), json.dumps(ONE_CAUSE), "JSON이 아니다",
                                  "[1, 2]", json.dumps({"summary": "x", "causes": ["a", "b", "c", "d"],
                                                        "checks": ["a", "b"]}),
                                  json.dumps({"summary": "漢字", "causes": ["a", "b"], "checks": ["a", "b"]})])
def test_parse_answer_variant_with_v1_schema_equals_analysis(text):
    assert variants.parse_answer_variant(text, analysis.ANALYSIS_SCHEMA) == analysis.parse_answer(text)


def test_schema_is_restored_after_call_and_after_exception(monkeypatch):
    original = analysis.ANALYSIS_SCHEMA
    variants.parse_answer_variant(json.dumps(ONE_CAUSE), variants.ANALYSIS_SCHEMA_V3)
    assert analysis.ANALYSIS_SCHEMA is original

    seen = []

    def boom(text):
        seen.append(analysis.ANALYSIS_SCHEMA)                # 호출 중에는 바꿔 둔 스키마가 보여야 한다
        raise RuntimeError("파싱 중 예외")

    monkeypatch.setattr(analysis, "parse_answer", boom)
    with pytest.raises(RuntimeError):
        variants.parse_answer_variant("x", variants.ANALYSIS_SCHEMA_V3)
    assert seen == [variants.ANALYSIS_SCHEMA_V3]
    assert analysis.ANALYSIS_SCHEMA is original


# ---------------------------------------------------------------- v3 메시지

def test_variant_v3_message_shape():
    ev = run_eval.build_prompt(_all(["s13"])[0], "")[0]
    none = variants.build_messages_variant(ev, "", "v3")
    assert [m["role"] for m in none] == ["system", "user"]
    assert none[0]["content"] == variants.SYSTEM_PROMPT_V3
    assert none[1]["content"] == analysis.mask(analysis.render_evidence(ev))
    with_rb = variants.build_messages_variant(ev, "[런북] 제목\n본문", "v3")
    assert with_rb[0]["content"] == variants.SYSTEM_PROMPT_V3 + "\n\n[런북] 제목\n본문"
    assert with_rb[1] == none[1]
    assert variants.build_messages_variant(ev, "", "v2")[1] == none[1]      # user는 v2와 같다


def test_variant_v3_shrinks_evidence_over_budget():
    ev = run_eval.build_prompt(_all(["s12"])[0], "")[0]
    ev["top_paths"] = ev["top_paths"] * 3
    big = "가" * (analysis.PROMPT_TOKEN_BUDGET - len(variants.SYSTEM_PROMPT_V3))
    got = variants.build_messages_variant(ev, big, "v3")[1]["content"]
    assert got == analysis.mask(analysis.render_evidence(analysis._shrunk(ev)))


def test_unknown_prompt_v4_rejected():
    ev = run_eval.build_prompt(_all(["s1"])[0], "")[0]
    with pytest.raises(ValueError):
        variants.build_messages_variant(ev, "", "v4")


# ---------------------------------------------------------------- 새 보류 시나리오

def test_holdout2_scenarios_load_with_flags_and_order():
    scs = _all()
    ids = [s["id"] for s in scs]
    assert ids == [*t5a.IDS, "s6_new_path_error", "s7_db_slow_combo", *t6a.HOLDOUT_IDS, *HOLDOUT2_IDS]
    new = [s for s in scs if s["id"] in HOLDOUT2_IDS]
    for s in new:
        assert s["holdout"] is True and s["holdout_set"] == 2 and "expect_v2" not in s
        assert s["baseline_rpm"] == 300 and s["incident"]["id"] == 1 and s["incident"]["opened_min_ago"] == 2
        assert set(s["expect"]) == {"cause_keywords", "check_keywords", "forbidden_keywords", "expect_unknown"}
    assert all("holdout_set" not in s for s in scs if s["id"] not in HOLDOUT2_IDS)      # s8~s11 파일은 그대로
    assert all(s.get("holdout") is True for s in scs if s["id"] in t6a.HOLDOUT_IDS)


def test_holdout2_scenario_details():
    s = {x["id"]: x for x in _all(HOLDOUT2_IDS)}
    assert [r["count"] for r in s["s12_new_exception"]["records"]] == [120, 80]
    assert s["s12_new_exception"]["records"][0]["error"] == "KeyError: 'amount'"
    assert s["s13_latency_unlocated"]["incident"]["rules"] == ["R2"]
    assert s["s13_latency_unlocated"]["incident"]["severity"] == "warning"
    assert s["s13_latency_unlocated"]["records"] == [
        {"path": "/", "status": 200, "count": 150, "latency_ms": 2600, "error": None}]
    assert s["s14_probe_refused"]["records_available"] is False and s["s14_probe_refused"]["records"] == []
    assert s["s14_probe_refused"]["probe"] == {"consecutive_failures": 2, "detail": "ConnectionRefusedError"}
    assert s["s15_timeout_error"]["records"][0]["latency_ms"] == 5000
    unknown = {k for k, x in s.items() if x["expect"]["expect_unknown"]}
    assert unknown == {"s13_latency_unlocated"}


@pytest.mark.parametrize("cond", THREE)
def test_holdout2_prompts_within_budget(cond):
    prompt, mode = run_eval.condition_spec(cond)
    rb_all = analysis.load_runbooks(t5a.RUNBOOK_DIR)
    system = {"v1": analysis.SYSTEM_PROMPT_V1, "v2": variants.SYSTEM_PROMPT_V2, "v3": variants.SYSTEM_PROMPT_V3}[prompt]
    for sc in _all(HOLDOUT2_IDS):
        rb = run_eval.runbook_text_for(mode, sc, rb_all, None)
        _, messages, _ = run_eval.build_prompt(sc, rb, prompt=prompt)
        assert run_eval.estimate_prompt_tokens(messages) <= analysis.PROMPT_TOKEN_BUDGET
        assert messages[0]["content"] == system + "\n\n" + rb


def test_s14_user_text_has_probe_reason_and_no_window_numbers():
    user = run_eval.build_prompt(_all(["s14"])[0], "")[1][1]["content"]
    assert "ConnectionRefusedError" in user and "[참고] 이 장애는 로그 창이 닫히기 전에 열려" in user
    assert "p95" not in user and "에러율" not in user


# ---------------------------------------------------------------- run_all 기록과 호출

class Chat:
    """answer_for(호출 번호) → 응답 dict 또는 글. fmt(스키마)도 기록한다."""

    def __init__(self, answer_for=None):
        self.calls: list = []
        self.answer_for = answer_for or (lambda n: TWO_CAUSE if n % 2 else ONE_CAUSE)

    def __call__(self, url, model, messages, options, fmt=None, keep_alive="24h", connect_timeout=5.0,
                 total_timeout=30.0):
        n = len(self.calls)
        self.calls.append({"messages": messages, "fmt": fmt})
        ans = self.answer_for(n)
        return t5a._result(text=ans) if isinstance(ans, str) else t5a._result(ans)


def _records(only=("s12", "s13"), conditions=THREE, chat=None, keywords="v2"):
    settings = run_eval.parse_settings("warm,var", run_eval.SETTINGS_V2)
    scs = _all(list(only), keywords)
    chat = chat or Chat()
    recs, aborted = run_eval.run_all(
        scs, analysis.load_runbooks(t5a.RUNBOOK_DIR), chat=chat, url="http://127.0.0.1:1", model="m",
        cfg=t5a.config.Config(), keep_alive="24h", connect_timeout=5.0, total_timeout=30.0, settings=settings,
        conditions=conditions)
    assert not aborted
    return scs, recs, chat


def test_run_all_passes_schema_per_prompt_and_records_new_fields():
    scs, recs, chat = _records()
    assert len(recs) == 2 * 3 * 9 == len(chat.calls)
    for rec, call in zip(recs, chat.calls, strict=True):
        want = variants.ANALYSIS_SCHEMA_V3 if rec["condition"] == "all_v3" else analysis.ANALYSIS_SCHEMA
        assert call["fmt"] is want
        assert rec["schema"] == ("v3" if rec["condition"] == "all_v3" else "v1")
        assert rec["prompt_version"] == run_eval.condition_spec(rec["condition"])[0]
        assert rec["holdout"] is True
    # 원인 1개 답(짝수 번 호출)은 v3 스키마에서만 파싱된다. v1·v2 조건에서는 형식 실패로 n_causes가 None이다.
    by = {(r["condition"], i): r for i, r in enumerate(recs)}
    one = [r for (c, i), r in by.items() if i % 2 == 0 and r["condition"] == "all_v3"]
    assert one and all(r["score"]["format_ok"] and r["n_causes"] == 1 and r["n_checks"] == 1 for r in one)
    old_one = [r for (c, i), r in by.items() if i % 2 == 0 and c != "all_v3"]
    assert old_one and all(not r["score"]["format_ok"] and r["n_causes"] is None for r in old_one)


def test_placeholder_copy_and_counts():
    _, recs, _ = _records(conditions=["all_v3"])
    for r in recs:
        two = r["parsed"] is not None and len(r["parsed"]["causes"]) == 2
        assert r["placeholder_copy"] is two                 # TWO_CAUSE 글에만 "알 수 없음: 이유"가 있다
        assert r["n_causes"] == (2 if two else 1) and r["n_checks"] == (2 if two else 1)
    chat = Chat(lambda n: "알 수 없음: 이유 JSON 아님")
    _, recs, _ = _records(only=("s13",), conditions=["all_v2"], chat=chat)
    assert all(r["placeholder_copy"] and r["n_causes"] is None and r["ok"] is True for r in recs)


def test_default_conditions_keep_v1_schema_and_old_behavior():
    scs = run_eval.load_scenarios(run_eval.SCENARIO_DIR, ["s1"])
    chat = Chat(lambda n: t5a.GOOD["s1_error_spike"])
    recs, _ = run_eval.run_all(scs, "", chat=chat, url="http://127.0.0.1:1", model="m", cfg=t5a.config.Config(),
                               keep_alive="24h", connect_timeout=5.0, total_timeout=30.0)
    assert {c["fmt"] is analysis.ANALYSIS_SCHEMA for c in chat.calls} == {True}
    assert {r["condition"] for r in recs} == {"no_runbook", "runbook"} and all(r["score"]["format_ok"] for r in recs)


def test_rescore_uses_prompt_schema():
    scs, recs, _ = _records(only=("s12",), conditions=["all_v3", "all_v2"])
    rows = [{k: v for k, v in r.items() if k != "parsed"} for r in recs]
    again = run_eval.rescore(rows, scs, analysis.load_runbooks(t5a.RUNBOOK_DIR))
    assert [r["score"] for r in again] == [r["score"] for r in recs]


# ---------------------------------------------------------------- 인자

def test_with_holdout2_flag():
    out: list = []
    code = run_eval.main(["--dry", "--with-holdout2", "--only", "s13,s14", "--conditions", "all_v3"],
                         out=out.append, scenario_dirs=(run_eval.SCENARIO_DIR,))
    text = "\n".join(out)
    assert code == 0 and "===== s13_latency_unlocated / all_v3 =====" in text and "s14_probe_refused / all_v3" in text
    assert "s8_db_partial_fail" not in text and "s1_error_spike" not in text
    out = []
    assert run_eval.main(["--dry", "--with-holdout", "--with-holdout2", "--only", "s9,s12", "--conditions", "all_v3"],
                         out=out.append, scenario_dirs=(run_eval.SCENARIO_DIR,)) == 0
    assert "s9_saturation / all_v3" in "\n".join(out) and "s12_new_exception / all_v3" in "\n".join(out)
    assert run_eval.main(["--dry", "--with-holdout2", "--holdout2", "/nonexistent-dir"], out=[].append) == 2


def test_without_flag_holdout2_is_not_loaded():
    out: list = []
    run_eval.main(["--dry", "--with-holdout", "--only", "s9"], out=out.append, scenario_dirs=(run_eval.SCENARIO_DIR,))
    assert run_eval.main(["--dry", "--only", "s12"], out=[].append, scenario_dirs=(run_eval.SCENARIO_DIR,)) == 2


# ---------------------------------------------------------------- 표

def _board(only, conditions=THREE, chat=None):
    scs, recs, _ = _records(only=only, conditions=conditions, chat=chat)
    meta = {**t5a.META, "compare": True, "compare_t6b": True, "system_sha256_v2": t6a.V2_SHA256,
            "system_sha256_v3": V3_SHA256, "title": "# 장애 분석 비교 채점표 (A1-T6b)"}
    return run_eval.render_scoreboard(meta, recs, scs), recs, scs


def test_t6b_tables_render():
    md, recs, scs = _board(("s1", "s8", "s12", "s13"))
    for head in ("## ⑫ ", "## ⑬ ", "## ⑭ ", "## ⑮ "):
        assert head in md
    assert md.index("## ⑦ ") < md.index("## ⑫ ")
    assert V3_SHA256 in md and "all_v3=v3/all" in md
    sec12 = md.split("## ⑫ ")[1].split("## ⑬ ")[0]
    assert "T6a에서 결과를 이미 봄" in sec12 and "새 보류 s12~s15" in sec12
    assert sec12.count("| all_v1 |") == 1 and sec12.count("| all_v3 |") == 1
    sec13 = md.split("## ⑬ ")[1].split("## ⑭ ")[0]
    assert ("| 조건 | 호출 | 형식 통과(format_ok) | 평균 원인 수 | 평균 확인 수 | 원인 1개 답 | placeholder_copy |"
            in sec13)
    row = next(x for x in sec13.splitlines() if x.startswith("| all_v3 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    n = 4 * 8                                              # 시나리오 4개 x (warm 3 + var 5)
    assert cells[1:3] == [str(n), f"{n}/{n}"]
    assert cells[3] == "1.50" and cells[4] == "1.50"       # 원인 1개와 2개 답이 번갈아 나온다
    assert cells[5] == str(n // 2) and cells[6] == str(n // 2)
    row = next(x for x in sec13.splitlines() if x.startswith("| all_v1 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[2] == f"{n // 2}/{n}"                     # v1 스키마는 원인 1개 답을 형식 실패로 센다
    sec14 = md.split("## ⑭ ")[1].split("## ⑮ ")[0]
    assert "s12_new_exception (보류 2)" in sec14 and "s8_db_partial_fail (보류)" in sec14
    assert "all_v3 warm" in sec14 and "all_v3 var" in sec14


def test_t6b_sheet_has_only_new_holdout_and_no_auto_line():
    md, _, _ = _board(("s1", "s8", "s12", "s13"))
    sec15 = md.split("## ⑮ ")[1]
    assert sec15.count("### s12_new_exception /") == 3 and sec15.count("### s13_latency_unlocated /") == 3
    assert "### s8_db_partial_fail" not in sec15 and "### s1_error_spike" not in sec15
    assert sec15.count("정확성 0~2: ____ / 유용성 0~2: ____") == 6
    assert "- 자동 판정" not in sec15
    assert "/payment 경로에서 KeyError 예외가 난다." in sec15 or "알 수 없음: 이유" in sec15      # 답 전문
    sec10 = md.split("## ⑩ ")[1].split("## ⑫ ")[0]
    assert "### s8_db_partial_fail" in sec10 and "s12_new_exception" not in sec10      # ⑩은 T6a 보류만


def test_t6a_tables_exclude_holdout2():
    md, recs, scs = _board(("s1", "s8", "s12"))
    sec7 = md.split("## ⑦ ")[1].split("## ⑧ ")[0]
    assert "설계 시나리오 1개, 보류 시나리오 1개" in sec7
    row = next(x for x in sec7.splitlines() if x.startswith("| all_v3 |"))
    assert "/8" in row and "/16" not in row                # 보류는 s8 하나(8번)뿐이다


def test_t6b_tables_absent_without_flag():
    scs, recs, _ = _records(only=("s12",), conditions=["all_v1"])
    md = run_eval.render_scoreboard({**t5a.META, "compare": True, "system_sha256_v2": t6a.V2_SHA256}, recs, scs)
    assert "## ⑩ " in md and "## ⑫ " not in md


def test_main_writes_t6b_outputs(tmp_path):
    chat = Chat(lambda n: ONE_CAUSE)
    out: list = []
    code = run_eval.main(["--out", str(tmp_path), "--with-holdout2", "--only", "s14", "--keywords", "v2",
                          "--conditions", "all_v1,all_v3", "--settings", "warm,var"],
                         chat=chat, version=lambda url: "0.40.2", out=out.append, now=NOW,
                         settings=run_eval.SETTINGS_V2, scenario_dirs=(run_eval.SCENARIO_DIR,))
    assert code == 0 and len(chat.calls) == 18
    raw = [json.loads(x) for x in (tmp_path / "raw" / "eval-20261105T080000Z.jsonl").read_text("utf-8").splitlines()]
    assert len(raw) == 18 and {r["condition"] for r in raw} == {"all_v1", "all_v3"}
    assert {r["schema"] for r in raw} == {"v1", "v3"} and all("placeholder_copy" in r for r in raw)
    md = (tmp_path / "scoreboard-20261105T080000Z.md").read_text("utf-8")
    assert "A1-T6b" in md and "## ⑮ " in md and "### s14_probe_refused / all_v3" in md and V3_SHA256 in md
