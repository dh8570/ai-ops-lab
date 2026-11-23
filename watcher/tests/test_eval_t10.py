"""A1-T10: 런북 원인 단위 조각(chunk_v1), 선택 규칙 select_chunks, 새 시나리오 s17~s19, 판정 지표 t10_metrics 시험.

기존 시험 파일은 수정하지 않고 test_eval의 도우미만 쓴다. Ollama는 부르지 않는다.
"""
import importlib
import json

import test_eval as t5a

analysis = t5a.analysis
run_eval = t5a.run_eval
variants = importlib.import_module("variants")
metrics = importlib.import_module("t10_metrics")

EVAL_DIR = run_eval.SCENARIO_DIR.parent
CHUNK_DIR = EVAL_DIR / "runbook_chunks"
REAL2 = EVAL_DIR / "scenarios_real2"
ALL_DIRS = [run_eval.SCENARIO_DIR, run_eval.EXTRA_SCENARIO_DIR, EVAL_DIR / "scenarios_holdout",
            EVAL_DIR / "scenarios_holdout2", EVAL_DIR / "scenarios_real", REAL2]
FOUR = ["nr_v1", "all_v1", "match_v1", "chunk_v1"]
OLD_SIX = ["nr_v1", "all_v1", "all_v2", "match_v1", "match_v2", "all_v3"]
ORIGINAL = t5a.RUNBOOK_DIR / "01_5xx_spike.md"


# ---------------------------------------------------------------- 조각 파일

def test_chunk_files_exist_and_first_line_format():
    names = sorted(p.name for p in CHUNK_DIR.glob("*.md"))
    assert names == ["01a_error_path.md", "01b_db_connection.md", "01z_generic.md"]
    for p in CHUNK_DIR.glob("*.md"):
        first = p.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# ") and not first.startswith(analysis.DRAFT_MARK)


def test_every_chunk_body_line_is_verbatim_in_original():
    original = ORIGINAL.read_text(encoding="utf-8").splitlines()
    for p in CHUNK_DIR.glob("*.md"):
        for line in p.read_text(encoding="utf-8").splitlines()[1:]:      # 제목 줄 제외
            assert line in original, (p.name, line)


def test_load_chunks_block_shape_matches_runbook_blocks():
    chunks = variants.load_chunks(CHUNK_DIR)
    assert list(chunks) == ["error_path", "db_connection", "generic"]
    assert all(t.startswith("[런북] 5xx 급증: ") for t in chunks.values())
    assert chunks["error_path"].splitlines()[1] == "자주 보는 원인"
    assert variants.load_chunks(CHUNK_DIR / "없음") == {}


# ---------------------------------------------------------------- 선택 규칙

def _ev(kinds=(), paths=(), rules=("R1",)):
    return {"incident": {"fired_rules": [{"rule": r, "detail": ""} for r in rules]},
            "top_error_kinds": [{"kind": k, "count": 10} for k in kinds],
            "top_paths": [{"path": p, "count": 10, "status_counts": {"2xx": 0, "4xx": 0, "5xx": n}}
                          for p, n in paths]}


def test_select_chunks_cases():
    sel = variants.select_chunks
    assert sel(_ev(["RuntimeError"], [("/boom", 5)])) == ["error_path"]
    assert sel(_ev(["OperationalError"], [("/orders", 5)])) == ["db_connection"]
    assert sel(_ev(["RuntimeError", "OperationalError"], [("/", 5)])) == ["error_path", "db_connection"]
    for kind in ("KeyError", "TimeoutError", "ValueError"):
        assert sel(_ev([kind], [("/x", 5)])) == ["generic"]
    assert sel(_ev([], [("/x", 5)])) == ["generic"]                   # 예외 정보 없음
    assert sel(_ev([], [("/", 0)])) == ["generic"]                    # 5xx 없음


def test_select_chunks_path_rule_and_db_kind_variants():
    sel = variants.select_chunks
    assert sel(_ev([], [("/error", 5)])) == ["error_path"]            # 경로 /error의 5xx
    assert sel(_ev([], [("/error", 0)])) == ["generic"]               # /error지만 5xx 없음
    assert sel(_ev(["psycopg2.OperationalError"])) == ["db_connection"]
    assert sel(_ev(["InterfaceError"])) == ["db_connection"]
    assert sel(_ev(["DatabaseError"])) == ["db_connection"]
    assert sel(_ev(["MyRuntimeError"])) == ["generic"]                # 정확히 RuntimeError만


def test_select_chunks_without_generic_gives_nothing():
    assert variants.select_chunks(_ev(["KeyError"]), available={"error_path", "db_connection"}) == []


def test_select_chunks_ignores_rule_ids():
    assert variants.select_chunks(_ev(["RuntimeError"], rules=("R2",))) == ["error_path"]


# ---------------------------------------------------------------- chunk 모드 조립

def test_chunk_mode_r2_r3_r4_use_same_blocks_as_match():
    blocks = variants.load_runbook_blocks(t5a.RUNBOOK_DIR)
    chunks = variants.load_chunks(CHUNK_DIR)
    for rule in ("R2", "R3", "R4"):
        text, ids = variants.chunk_runbook_text(_ev(["KeyError"], rules=(rule,)), blocks, chunks)
        assert ids == []
        assert text == variants.select_runbooks(blocks, {rule})
    text, ids = variants.chunk_runbook_text(_ev(["RuntimeError"], rules=("R1", "R2")), blocks, chunks)
    assert ids == ["error_path"]
    assert text == chunks["error_path"] + "\n\n" + variants.select_runbooks(blocks, {"R2"})


def test_chunk_mode_without_any_rule_is_empty():
    text, ids = variants.chunk_runbook_text(_ev(["RuntimeError"], rules=()), [], variants.load_chunks(CHUNK_DIR))
    assert (text, ids) == ("", [])


def test_chunk_condition_registered_and_dispatch():
    assert variants.CONDITIONS["chunk_v1"] == ("v1", "chunk")
    assert run_eval.condition_spec("chunk_v1") == ("v1", "chunk")
    sc = next(s for s in run_eval.load_scenarios(ALL_DIRS) if s["id"] == "s19_real_shape_other_path")
    text, ids = run_eval.runbook_and_chunks_for("chunk", sc, "", [], variants.load_chunks(CHUNK_DIR))
    assert ids == ["error_path"] and "RuntimeError로 500" in text and "DB" not in text


def test_scenario_chunk_choices():
    chunks = variants.load_chunks(CHUNK_DIR)
    blocks = variants.load_runbook_blocks(t5a.RUNBOOK_DIR)
    got = {sc["id"]: run_eval.runbook_and_chunks_for("chunk", sc, "", blocks, chunks)[1]
           for sc in run_eval.load_scenarios(ALL_DIRS)}
    assert got["s16_real_error_shape"] == ["error_path"]
    assert got["s17_db_new_path"] == ["db_connection"]
    assert got["s18_new_exception_report"] == ["generic"]
    assert got["s12_new_exception"] == ["generic"]
    assert got["s3_latency"] == []                                      # R1이 없으면 R1 조각은 없다


# ---------------------------------------------------------------- 기존 조건 불변

def _old_build_prompt(scenario, runbooks_text, now_ts, prompt):
    """변경 전 build_prompt를 그대로 다시 계산한다(T10 이전 코드)."""
    records = run_eval.expand_records(scenario, now_ts) if scenario["records_available"] else None
    window = run_eval.stats.summarize(records or [], run_eval.WINDOW_SEC, run_eval.EXCLUDE_PATHS)
    evidence = analysis.build_evidence(
        run_eval.make_incident(scenario, now_ts), window, records, scenario["baseline_rpm"], scenario["probe"],
        now_ts, window_sec=run_eval.WINDOW_SEC, exclude_paths=run_eval.EXCLUDE_PATHS, runbooks_text=runbooks_text,
        current_rules=scenario["current_rules"])
    return variants.build_messages_variant(evidence, runbooks_text, prompt)


def _old_runbook_text(mode, scenario, all_text, blocks):
    return {"none": "", "all": all_text}.get(mode) if mode in ("none", "all") else \
        variants.select_runbooks(blocks or [], scenario["current_rules"])


def test_existing_conditions_messages_unchanged():
    all_text = analysis.load_runbooks(t5a.RUNBOOK_DIR)
    blocks = variants.load_runbook_blocks(t5a.RUNBOOK_DIR)
    for sc in run_eval.load_scenarios(ALL_DIRS):
        for cond in OLD_SIX:
            prompt, mode = run_eval.condition_spec(cond)
            old_rb = _old_runbook_text(mode, sc, all_text, blocks)
            new_rb = run_eval.runbook_text_for(mode, sc, all_text, blocks)
            assert new_rb == old_rb
            new_msgs = run_eval.build_prompt(sc, new_rb, run_eval.NOW_TS, prompt)[1]
            assert new_msgs == _old_build_prompt(sc, old_rb, run_eval.NOW_TS, prompt), (sc["id"], cond)


# ---------------------------------------------------------------- 새 시나리오

def test_s17_to_s19_load_with_expected_shape():
    scs = run_eval.load_scenarios(REAL2)
    assert [s["id"] for s in scs] == ["s17_db_new_path", "s18_new_exception_report", "s19_real_shape_other_path"]
    for s in scs:
        assert s["holdout"] is True and s["holdout_set"] == 3
        assert s["incident"] == {"id": 1, "severity": "critical", "rules": ["R1"], "opened_min_ago": 2}
        assert s["current_rules"] == ["R1"] and s["baseline_rpm"] == 300
        assert s["probe"] == {"consecutive_failures": 0, "detail": "HTTP 200"}
        assert s["expect"]["expect_unknown"] is False
    assert len(run_eval.load_scenarios(ALL_DIRS)) == 19


def test_new_scenarios_prompt_tokens_within_3000_for_four_conditions():
    all_text = analysis.load_runbooks(t5a.RUNBOOK_DIR)
    blocks = variants.load_runbook_blocks(t5a.RUNBOOK_DIR)
    chunks = variants.load_chunks(CHUNK_DIR)
    for sc in run_eval.load_scenarios(REAL2):
        for cond in FOUR:
            prompt, mode = run_eval.condition_spec(cond)
            rb = run_eval.runbook_text_for(mode, sc, all_text, blocks, chunks)
            msgs = run_eval.build_prompt(sc, rb, run_eval.NOW_TS, prompt)[1]
            assert run_eval.estimate_prompt_tokens(msgs) <= 3000, (sc["id"], cond)


def test_run_all_records_chunks_and_scoreboard_has_chunk_table():
    class Chat:
        def __call__(self, url, model, messages, options, fmt=None, keep_alive="24h", connect_timeout=5.0,
                     total_timeout=30.0):
            return t5a._result({"summary": "요약", "causes": ["/boom 경로의 예외", "알 수 없음: 근거 부족"],
                                "checks": ["docker compose logs app를 본다.", "경로를 확인한다."]})
    scs = run_eval.load_scenarios([REAL2], ["s17", "s19"], "v2")
    settings = run_eval.parse_settings("warm", run_eval.SETTINGS_V2)
    recs, aborted = run_eval.run_all(
        scs, analysis.load_runbooks(t5a.RUNBOOK_DIR), chat=Chat(), url="http://127.0.0.1:1", model="m",
        cfg=t5a.config.Config(), keep_alive="24h", connect_timeout=5.0, total_timeout=30.0, settings=settings,
        conditions=FOUR, blocks=variants.load_runbook_blocks(t5a.RUNBOOK_DIR))
    assert not aborted
    by = {(r["scenario"], r["condition"]): r["chunks"] for r in recs}
    assert by[("s17_db_new_path", "chunk_v1")] == ["db_connection"]
    assert by[("s19_real_shape_other_path", "chunk_v1")] == ["error_path"]
    assert all(r["chunks"] == [] for r in recs if r["condition"] != "chunk_v1")
    md = run_eval.render_scoreboard({**t5a.META, "compare": True, "system_sha256_v2": "x", "keywords": "v2"}, recs, scs)
    assert "## ⑯ 시나리오 x 선택된 조각 id" in md and "db_connection" in md
    assert "(보류 3)" in md


# ---------------------------------------------------------------- t10_metrics

def _rec(scenario, cond, setting, rep, causes, strict=True, ok=True):
    parsed = {"ok": ok, "causes": causes} if ok else None
    return {"scenario": scenario, "condition": cond, "setting": setting, "rep": rep, "parsed": parsed,
            "score": {"strict_v3_passed": strict}}


def test_metrics_functions():
    assert metrics.db_in_causes(_rec("s", "c", "warm", 1, ["DB 연결 문제"]))
    assert metrics.db_in_causes(_rec("s", "c", "warm", 1, ["x", "데이터베이스 지연"]))
    assert not metrics.db_in_causes(_rec("s", "c", "warm", 1, ["/error 예외"]))
    assert not metrics.db_in_causes(_rec("s", "c", "warm", 1, [], ok=False))
    r = _rec("s16_real_error_shape", "c", "warm", 1, ["/error의 예외", "DB 문제"])
    assert metrics.first_cause_ok(r, ["/error", "RuntimeError"])
    assert not metrics.first_cause_ok(_rec("s", "c", "warm", 1, ["DB 문제", "/error"]), ["/error"])
    assert not metrics.first_cause_ok(_rec("s", "c", "warm", 1, [], ok=False), ["/error"])


def test_metrics_samples_take_one_warm_and_all_var():
    rows = [_rec("s1", "c", "warmup", 1, ["a"])] + [_rec("s1", "c", "warm", i, ["a"]) for i in (1, 2, 3)] \
        + [_rec("s1", "c", "var", i, ["a"]) for i in range(1, 6)]
    assert len(metrics.samples(rows)[("s1", "c")]) == 6


def test_metrics_report_numbers():
    rows = []
    for cond, db in (("all_v1", True), ("chunk_v1", False)):
        for sid in metrics.S_NONDB:
            rows += [_rec(sid, cond, "warm", i, ["/x 예외", "DB 연결 문제"] if db else ["/x 예외"]) for i in (1, 2, 3)]
            rows += [_rec(sid, cond, "var", i, ["/x 예외", "DB 연결 문제"] if db else ["/x 예외"]) for i in range(1, 6)]
    out = metrics.report(rows, {"s12_new_exception": ["/payment"]})
    assert "| all_v1 | 30/30 (100.0%) |" in out and "| chunk_v1 | 0/30 (0.0%) |" in out
    assert "기준1" in out and "기준5" in out and "합격" not in out
    assert json.loads((EVAL_DIR / "truth_tokens.json").read_text(encoding="utf-8"))["s16_real_error_shape"] == [
        "/error", "RuntimeError"]
