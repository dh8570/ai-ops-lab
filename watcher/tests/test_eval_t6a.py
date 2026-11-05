"""A1-T6a: 프롬프트 v2·런북 선택 조건(variants.py), 보류 시나리오 s8~s11, run_eval의 --conditions·--settings·
--with-holdout, 비교표 ⑦~⑩ 시험.

기존 시험 파일(test_eval.py, test_eval_t5b.py)은 수정하지 않고 도우미만 가져다 쓴다. Ollama는 부르지 않는다.
"""
import hashlib
import importlib
import json
from datetime import UTC, datetime

import pytest
import test_eval as t5a

analysis = t5a.analysis
scoring, run_eval = t5a.scoring, t5a.run_eval
variants = importlib.import_module("variants")

EVAL_DIR = run_eval.SCENARIO_DIR.parent
HOLDOUT = EVAL_DIR / "scenarios_holdout"
ALL_DIRS = [run_eval.SCENARIO_DIR, run_eval.EXTRA_SCENARIO_DIR, HOLDOUT]
HOLDOUT_IDS = ["s8_db_partial_fail", "s9_saturation", "s10_5xx_no_detail", "s11_probe_timeout"]
FIVE = ["nr_v1", "all_v1", "all_v2", "match_v1", "match_v2"]
NOW = datetime(2026, 11, 3, 8, 0, 0, tzinfo=UTC)

V2_SHA256 = "f4575ffd2359e617b29bfe85187b30da10eb125a36b296199e3ff3ca0e8d6af8"


def _all(only=None, keywords="v1"):
    return run_eval.load_scenarios(ALL_DIRS, only, keywords)


def _evidence(sc):
    return run_eval.build_prompt(sc, "")[0]


@pytest.fixture(scope="module")
def blocks():
    return variants.load_runbook_blocks(t5a.RUNBOOK_DIR)


@pytest.fixture(scope="module")
def runbooks_text():
    return analysis.load_runbooks(t5a.RUNBOOK_DIR)


# ---------------------------------------------------------------- variants

def test_system_prompt_v2_is_pinned():
    assert hashlib.sha256(variants.SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest() == V2_SHA256
    assert variants.SYSTEM_PROMPT_V2 != analysis.SYSTEM_PROMPT_V1


def test_conditions_table():
    assert variants.CONDITIONS == {"nr_v1": ("v1", "none"), "all_v1": ("v1", "all"), "all_v2": ("v2", "all"),
                                   "match_v1": ("v1", "match"), "match_v2": ("v2", "match"),
                                   "all_v3": ("v3", "all")}


@pytest.mark.parametrize("rb", ["", "[런북] 제목\n본문"])
def test_variant_v1_is_byte_identical_to_analysis(rb):
    for sc in _all():
        ev = _evidence(sc)
        assert variants.build_messages_variant(ev, rb, "v1") == analysis.build_messages(ev, rb)


def test_variant_v1_shrinks_like_analysis_when_over_budget():
    sc = _all(["s6"])[0]
    ev = _evidence(sc)
    big = "가" * 2990                                      # 근거와 합치면 추정 3000을 넘는다
    assert variants.build_messages_variant(ev, big, "v1") == analysis.build_messages(ev, big)


def test_variant_v2_message_shape():
    sc = _all(["s9"])[0]
    ev = _evidence(sc)
    none = variants.build_messages_variant(ev, "", "v2")
    assert [m["role"] for m in none] == ["system", "user"]
    assert none[0]["content"] == variants.SYSTEM_PROMPT_V2
    assert none[1]["content"] == analysis.mask(analysis.render_evidence(ev))
    with_rb = variants.build_messages_variant(ev, "[런북] 제목\n본문", "v2")
    assert with_rb[0]["content"] == variants.SYSTEM_PROMPT_V2 + "\n\n[런북] 제목\n본문"
    assert with_rb[1] == none[1]


def test_variant_v2_shrinks_evidence_over_budget():
    ev = _evidence(_all(["s8"])[0])
    assert len(ev["top_paths"]) == 2                        # s8은 경로 2개(/, /checkout)
    ev["top_paths"] = ev["top_paths"] * 3                   # 6개로 늘려 축소 규칙(3개)이 보이게 한다
    small = variants.build_messages_variant(ev, "", "v2")[1]["content"]
    over = variants.build_messages_variant(ev, "가" * (analysis.PROMPT_TOKEN_BUDGET - len(variants.SYSTEM_PROMPT_V2)),
                                           "v2")[1]["content"]
    assert small.count("/checkout") > over.count("/checkout") or len(over) < len(small)
    assert over == analysis.mask(analysis.render_evidence(analysis._shrunk(ev)))


def test_variant_unknown_prompt_rejected():
    with pytest.raises(ValueError):
        variants.build_messages_variant(_evidence(_all(["s1"])[0]), "", "v4")


def test_blocks_join_equals_load_runbooks(blocks, runbooks_text):
    assert "\n\n".join(b["text"] for b in blocks) == runbooks_text
    assert [b["rules"] for b in blocks] == [{"R1"}, {"R2"}, {"R4"}]
    assert all(b["text"].startswith("[런북] ") and "초안:" not in b["text"] for b in blocks)


def test_blocks_skip_draft_lines_and_missing_dir(tmp_path):
    (tmp_path / "a.md").write_text("초안: 검토\n\n# 제목 (규칙 R3, R2)\n- 본문\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("초안: 검토\n", encoding="utf-8")             # 제목이 없어 건너뜀
    (tmp_path / "c.md").write_text("제목만 (괄호 없음)\n", encoding="utf-8")
    got = variants.load_runbook_blocks(tmp_path)
    assert got == [{"text": "[런북] 제목 (규칙 R3, R2)\n- 본문", "rules": {"R2", "R3"}},
                   {"text": "[런북] 제목만 (괄호 없음)", "rules": set()}]
    assert "\n\n".join(b["text"] for b in got) == analysis.load_runbooks(tmp_path)
    assert variants.load_runbook_blocks(tmp_path / "없음") == []


@pytest.mark.parametrize("rules,expect_titles", [
    (["R1"], ["5xx"]), (["R2"], ["응답 지연"]), (["R4"], ["앱 무응답"]), (["R3"], []),
    (["R1", "R2"], ["5xx", "응답 지연"]), ([], []), (["R2", "R3"], ["응답 지연"])])
def test_select_runbooks(blocks, runbooks_text, rules, expect_titles):
    got = variants.select_runbooks(blocks, rules)
    if not expect_titles:
        assert got == ""
        return
    assert got.count("[런북] ") == len(expect_titles)
    for t in expect_titles:
        assert t in got
    assert got in runbooks_text or len(expect_titles) == 2      # 파일 순서가 같다(둘이면 아래 순서 확인)
    if len(expect_titles) == 2:
        assert got.index(expect_titles[0]) < got.index(expect_titles[1])
        assert got == runbooks_text.split("\n\n[런북] 앱 무응답")[0]


# ---------------------------------------------------------------- 보류 시나리오

def test_holdout_scenarios_load_with_flag_and_natural_order():
    scs = _all()
    assert [s["id"] for s in scs] == [*t5a.IDS, "s6_new_path_error", "s7_db_slow_combo", *HOLDOUT_IDS]
    held = [s for s in scs if s.get("holdout")]
    assert [s["id"] for s in held] == HOLDOUT_IDS
    assert not any(s.get("holdout") for s in scs if s["id"] not in HOLDOUT_IDS)
    for s in held:
        assert s["baseline_rpm"] == 300 and s["incident"]["id"] == 1 and s["incident"]["opened_min_ago"] == 2
        assert set(s["expect"]) == {"cause_keywords", "check_keywords", "forbidden_keywords", "expect_unknown"}


def test_holdout_scenario_details():
    s = {x["id"]: x for x in _all(HOLDOUT_IDS)}
    assert s["s8_db_partial_fail"]["current_rules"] == ["R1"]
    assert sum(r["count"] for r in s["s8_db_partial_fail"]["records"] if r["status"] == 500) == 250
    assert s["s9_saturation"]["incident"]["rules"] == ["R2", "R3"]
    assert s["s9_saturation"]["incident"]["severity"] == "warning"
    assert s["s10_5xx_no_detail"]["expect"]["expect_unknown"] is True
    assert s["s10_5xx_no_detail"]["records"][1]["error"] is None
    assert s["s11_probe_timeout"]["records_available"] is False and s["s11_probe_timeout"]["records"] == []
    assert s["s11_probe_timeout"]["probe"] == {"consecutive_failures": 2, "detail": "timeout"}
    assert not any(x["expect"]["expect_unknown"] for k, x in s.items() if k != "s10_5xx_no_detail")


def test_load_scenarios_only_matches_s1_not_s10_s11():
    assert [s["id"] for s in _all(["s1"])] == ["s1_error_spike"]
    assert [s["id"] for s in _all(["s10", "s11"])] == ["s10_5xx_no_detail", "s11_probe_timeout"]


@pytest.mark.parametrize("cond", FIVE)
def test_holdout_prompts_within_budget_for_all_conditions(cond, blocks, runbooks_text):
    prompt, mode = run_eval.condition_spec(cond)
    for sc in _all(HOLDOUT_IDS):
        rb = run_eval.runbook_text_for(mode, sc, runbooks_text, blocks)
        _, messages, _ = run_eval.build_prompt(sc, rb, prompt=prompt)
        assert run_eval.estimate_prompt_tokens(messages) <= analysis.PROMPT_TOKEN_BUDGET
        system = variants.SYSTEM_PROMPT_V2 if prompt == "v2" else analysis.SYSTEM_PROMPT_V1
        assert messages[0]["content"] == system + ("\n\n" + rb if rb else "")


def test_holdout_s11_user_text_has_no_window_numbers():
    sc = _all(["s11"])[0]
    user = run_eval.build_prompt(sc, "")[1][1]["content"]
    assert "[참고] 이 장애는 로그 창이 닫히기 전에 열려" in user
    assert "에러율" not in user.split("[기준선]")[0]
    assert "timeout" in user


def test_match_runbooks_follow_current_rules(blocks, runbooks_text):
    by = {s["id"]: s for s in _all()}
    assert run_eval.runbook_text_for("match", by["s1_error_spike"], runbooks_text, blocks).count("[런북]") == 1
    assert run_eval.runbook_text_for("match", by["s9_saturation"], runbooks_text, blocks).count("[런북]") == 1
    assert "응답 지연" in run_eval.runbook_text_for("match", by["s9_saturation"], runbooks_text, blocks)
    assert "앱 무응답" in run_eval.runbook_text_for("match", by["s11_probe_timeout"], runbooks_text, blocks)
    assert run_eval.runbook_text_for("match", by["s7_db_slow_combo"], runbooks_text, blocks).count("[런북]") == 2
    assert run_eval.runbook_text_for("all", by["s1_error_spike"], runbooks_text, blocks) == runbooks_text
    assert run_eval.runbook_text_for("none", by["s1_error_spike"], runbooks_text, blocks) == ""


# ---------------------------------------------------------------- 별칭과 인자

def test_condition_aliases_and_parsing():
    assert run_eval.condition_spec("no_runbook") == ("v1", "none")
    assert run_eval.condition_spec("runbook") == ("v1", "all")
    assert run_eval.condition_spec("match_v2") == ("v2", "match")
    assert run_eval.parse_conditions(None) == ("no_runbook", "runbook") == run_eval.CONDITIONS
    assert run_eval.parse_conditions("nr_v1, all_v2 ,match_v1") == ("nr_v1", "all_v2", "match_v1")
    with pytest.raises(ValueError):
        run_eval.parse_conditions("nr_v1,bogus")


def test_parse_settings():
    f = run_eval.parse_settings
    got = f("warm,var", run_eval.SETTINGS_V2)
    assert [s[0] for s in got] == ["warmup", "warm", "warm", "warm", "var", "var", "var", "var", "var"]
    assert [s[0] for s in f("prod", run_eval.SETTINGS_V2)] == ["prod"] * 3
    assert [s[0] for s in f("var", run_eval.SETTINGS_V2)] == ["var"] * 5
    with pytest.raises(ValueError):
        f("warm,hot", run_eval.SETTINGS_V2)
    with pytest.raises(ValueError):
        f("warm", run_eval.SETTINGS)                        # SETTINGS(T5a)에는 warm이 없다


def test_default_conditions_and_scenarios_are_unchanged():
    out: list = []
    assert run_eval.main(["--dry", "--only", "s1"], out=out.append, scenario_dirs=(run_eval.SCENARIO_DIR,)) == 0
    text = "\n".join(out)
    assert "s1_error_spike / no_runbook" in text and "s1_error_spike / runbook" in text
    assert "s8" not in text and "nr_v1" not in text


def test_with_holdout_flag_adds_holdout_scenarios():
    out: list = []
    code = run_eval.main(["--dry", "--with-holdout", "--only", "s9,s10",
                          "--conditions", "all_v2,match_v2"], out=out.append, scenario_dirs=(run_eval.SCENARIO_DIR,))
    text = "\n".join(out)
    assert code == 0
    assert "===== s9_saturation / all_v2 =====" in text and "===== s10_5xx_no_detail / match_v2 =====" in text
    assert "===== s9_saturation / nr_v1" not in text


def test_bad_conditions_or_settings_exit_2():
    out: list = []
    assert run_eval.main(["--dry", "--conditions", "zzz"], out=out.append) == 2
    assert run_eval.main(["--dry", "--settings", "zzz"], out=out.append) == 2
    assert run_eval.main(["--dry", "--with-holdout", "--holdout", "/nonexistent-dir"], out=out.append) == 2


# ---------------------------------------------------------------- 실행과 표

def _answer(_sid=None):
    return {"summary": "app 컨테이너가 /slow 요청으로 응답하지 않는다.",
            "causes": ["app 워커가 /slow 때문에 포화다.", "알 수 없음: 로그가 없다."],
            "checks": ["docker compose ps로 app을 본다.", "docker compose logs app을 본다."]}


class Chat:
    def __init__(self):
        self.calls: list = []

    def __call__(self, url, model, messages, options, fmt=None, keep_alive="24h", connect_timeout=5.0,
                 total_timeout=30.0):
        self.calls.append(messages)
        return t5a._result(_answer())


def _records(only=("s8", "s9"), conditions=FIVE, settings=None):
    settings = settings or run_eval.parse_settings("warm,var", run_eval.SETTINGS_V2)
    scs = _all(list(only), "v2")
    chat = Chat()
    runbooks_text = analysis.load_runbooks(t5a.RUNBOOK_DIR)
    recs, aborted = run_eval.run_all(
        scs, runbooks_text, chat=chat, url="http://127.0.0.1:1", model="m", cfg=t5a.config.Config(),
        keep_alive="24h", connect_timeout=5.0, total_timeout=30.0, settings=settings, conditions=conditions,
        blocks=variants.load_runbook_blocks(t5a.RUNBOOK_DIR))
    assert not aborted
    return scs, recs, chat


def test_run_all_five_conditions_records_variant_fields():
    scs, recs, chat = _records()
    assert len(recs) == 2 * 5 * 9 and len(chat.calls) == 90
    first = {(r["scenario"], r["condition"]): r for r in recs if r["setting"] == "warmup"}
    assert first[("s9_saturation", "nr_v1")]["runbook_mode"] == "none"
    assert first[("s9_saturation", "nr_v1")]["runbook_chars"] == 0
    assert first[("s9_saturation", "all_v2")]["prompt_version"] == "v2"
    assert first[("s9_saturation", "all_v2")]["runbook_chars"] == len(analysis.load_runbooks(t5a.RUNBOOK_DIR))
    m = first[("s9_saturation", "match_v2")]
    assert 0 < m["runbook_chars"] < first[("s9_saturation", "all_v2")]["runbook_chars"]
    assert all(r["holdout"] is True for r in recs)
    sys_sha = {r["condition"]: r["system_sha256"] for r in recs if r["scenario"] == "s9_saturation"}
    assert sys_sha["nr_v1"] != sys_sha["all_v1"] != sys_sha["all_v2"]
    assert sys_sha["nr_v1"] == hashlib.sha256(analysis.SYSTEM_PROMPT_V1.encode()).hexdigest()


def test_run_all_default_conditions_keep_old_names():
    scs = run_eval.load_scenarios(run_eval.SCENARIO_DIR, ["s1"])
    recs, _ = run_eval.run_all(scs, "[런북] x\n본문", chat=Chat(), url="http://127.0.0.1:1", model="m",
                               cfg=t5a.config.Config(), keep_alive="24h", connect_timeout=5.0, total_timeout=30.0)
    assert {r["condition"] for r in recs} == {"no_runbook", "runbook"} and len(recs) == 16
    assert {r["holdout"] for r in recs} == {False}


def test_compare_tables_render():
    scs, recs, _ = _records(only=("s1", "s8", "s9"))
    meta = {**t5a.META, "compare": True, "system_sha256_v2": V2_SHA256, "title": "# 장애 분석 비교 채점표 (A1-T6a)"}
    md = run_eval.render_scoreboard(meta, recs, scs)
    for head in ("## ⑦ ", "## ⑧ ", "## ⑨ ", "## ⑩ "):
        assert head in md
    assert V2_SHA256 in md and "all_v2=v2/all" in md
    sec7 = md.split("## ⑦ ")[1].split("## ⑧ ")[0]
    assert "| 조건 | 설계 passed | 설계 strict_v3 | 보류 passed | 보류 strict_v3 |" in sec7
    assert "| nr_v1 | 8/8 |" in sec7 or "| nr_v1 |" in sec7                # 설계 s1 하나 x (warm 3 + var 5)
    sec8 = md.split("## ⑧ ")[1].split("## ⑨ ")[0]
    assert "s8_db_partial_fail (보류)" in sec8 and "nr_v1 warm" in sec8 and "match_v2 var" in sec8
    sec9 = md.split("## ⑨ ")[1].split("## ⑩ ")[0]
    assert "| match_v2 | 24 |" in sec9                                        # 3 시나리오 x (warm 3 + var 5)
    sec10 = md.split("## ⑩ ")[1]
    assert sec10.count("### s8_db_partial_fail /") == 5 and sec10.count("### s9_saturation /") == 5
    assert "### s1_error_spike" not in sec10                                 # 시트는 보류 시나리오만
    assert sec10.count("정확성 0~2: ____ / 유용성 0~2: ____") == 10
    assert "- 자동 판정" not in sec10                                       # 제목 문구와 달리 판정 줄은 없다
    assert "app 컨테이너가 /slow 요청으로 응답하지 않는다." in sec10         # 답 전문


def test_compare_tables_pool_warm_and_var_not_warmup():
    scs, recs, _ = _records(only=("s9",), conditions=["nr_v1"])
    for r in recs:                                       # 모든 호출이 strict_v3 통과이게 만든 뒤 warmup만 뒤집는다
        r["score"]["passed"] = r["score"]["strict_v3_passed"] = True
        if r["setting"] == "warmup":
            r["score"]["passed"] = r["score"]["strict_v3_passed"] = False
    lines = run_eval._compare_tables(recs, scs, ("nr_v1",))
    row = next(x for x in lines if x.startswith("| nr_v1 | -"))
    assert row == "| nr_v1 | - | - | 8/8 | 8/8 |"           # 설계 시나리오가 없으면 '-', warmup 1번은 빠진다


def test_scoreboard_without_compare_has_no_new_tables_and_old_headings():
    scs = run_eval.load_scenarios(run_eval.SCENARIO_DIR, ["s1"])
    recs, _ = run_eval.run_all(scs, "", chat=Chat(), url="http://127.0.0.1:1", model="m", cfg=t5a.config.Config(),
                               keep_alive="24h", connect_timeout=5.0, total_timeout=30.0)
    md = run_eval.render_scoreboard(t5a.META, recs, scs)
    assert "## ⑧ " not in md and "## ⑩ " not in md and "## ⑥ " in md


def test_main_writes_compare_outputs(tmp_path):
    chat = Chat()
    out: list = []
    code = run_eval.main(["--out", str(tmp_path), "--with-holdout", "--only", "s11", "--keywords", "v2",
                          "--conditions", "nr_v1,match_v2", "--settings", "warm,var"],
                         chat=chat, version=lambda url: "0.40.2", out=out.append, now=NOW,
                         settings=run_eval.SETTINGS_V2, scenario_dirs=(run_eval.SCENARIO_DIR,))
    assert code == 0 and len(chat.calls) == 18
    raw = [json.loads(x) for x in (tmp_path / "raw" / "eval-20261103T080000Z.jsonl").read_text("utf-8").splitlines()]
    assert len(raw) == 18 and {r["condition"] for r in raw} == {"nr_v1", "match_v2"}
    assert {r["setting"] for r in raw} == {"warmup", "warm", "var"}
    md = (tmp_path / "scoreboard-20261103T080000Z.md").read_text("utf-8")
    assert "## ⑩ " in md and "A1-T6a" in md and "### s11_probe_timeout / match_v2" in md
