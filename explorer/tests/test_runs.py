"""실행 기록(runs.py): run_id, 원자적 쓰기, 인덱스, 크기 제한, validate_bundle, 오류·중단 때의 단계 상태."""
import copy
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

import runs

TOKENIZER = {"repo": "Qwen/Qwen2.5-7B-Instruct", "revision": "a" * 40, "sha256": "b" * 64}
OPTIONS = {"temperature": 0.2, "num_predict": 200, "num_ctx": 4096}


def _tok(index, token_id, piece, hex_):
    return {"index": index, "id": token_id, "piece": piece, "hex": hex_, "valid_utf8": True, "partial": False,
            "special": False, "fragment_of": []}


TOK = {"text_chars": 2, "text_bytes": 6, "nfc_changed": False, "token_count": 2, "partial_count": 0,
       "bytes_match": True, "tokens": [_tok(0, 1, "안", "ec 95 88"), _tok(1, 2, "녕", "eb 85 95")]}
CHUNKS = [{"type": "chunk", "n": 1, "text": "안", "gap_ms": 20.0, "since_start_ms": 20.0},
          {"type": "chunk", "n": 2, "text": "녕", "gap_ms": 10.0, "since_start_ms": 30.0}]
STATS = {"total_duration": 3_000_000_000, "load_duration": 1_000_000_000, "prompt_eval_count": 31,
         "prompt_eval_cached_count": 0, "prompt_eval_duration": 500_000_000, "eval_count": 2,
         "eval_duration": 1_500_000_000, "done_reason": "stop"}
DONE = {"type": "done", "chunks": 2, "joined": "안녕", "final_response": "", "since_start_ms": 31.0, "stats": STATS}
T0 = datetime(2026, 10, 28, 6, 45, 12, 345000, tzinfo=UTC)


def make(*, chunks=CHUNKS, done=DONE, status="ok", error=None, run_id="r-20261028-064512-ab12", **kw):
    return runs.build_playground_record(
        run_id=run_id, created_at=runs.utc_iso(T0), prompt="안녕", model="fake:1b", options=OPTIONS,
        ollama_version="0.0.test", tokenizer=TOKENIZER, tok_result=TOK, tok_ms=1.5,
        chunks=chunks, done=done, status=status, error=error, **kw)


# ---- run_id
def test_run_id_shape_and_time():
    rid = runs.make_run_id(T0)
    assert runs.RUN_ID_RE.fullmatch(rid) and rid.startswith("r-20261028-064512-")
    assert runs.utc_iso(T0) == "2026-10-28T06:45:12.345Z"


BAD_IDS = ["R-20261028-064512-ab12", "r-20261028-064512-AB12", "r-20261028-064512-ab1", "r-2026102-064512-ab12",
           "r-20261028-064512-ab12x", "../r-20261028-064512-ab12", "r-20261028-064512-gggg", ""]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_run_id_regex_rejects(bad):
    assert not runs.RUN_ID_RE.fullmatch(bad)


def test_run_id_is_regenerated_when_the_name_exists():
    tokens = iter(["aaaa", "aaaa", "bbbb"])
    seen = []

    def exists(rid):
        seen.append(rid)
        return rid.endswith("aaaa")
    rid = runs.make_run_id(T0, exists=exists, token=lambda: next(tokens))
    assert rid.endswith("bbbb") and len(seen) == 3


# ---- 쓰기
def test_write_creates_run_file_and_index_without_temp_files(tmp_path):
    rec = make()
    assert runs.write_run(tmp_path, rec) is True
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == [f"{rec['run_id']}.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.json", "runs"]               # .tmp-* 파일이 남지 않음
    assert json.loads((tmp_path / "runs" / f"{rec['run_id']}.json").read_text(encoding="utf-8")) == rec
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == [runs.index_entry(rec)]


def test_failed_replace_leaves_no_temp_file_and_no_partial_file(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    assert runs.write_run(tmp_path, make()) is False                       # 예외를 밖으로 내보내지 않는다
    assert [p.name for p in (tmp_path / "runs").iterdir()] == []            # 반쯤 쓰인 파일도, 임시 파일도 없음
    assert "disk full" in capsys.readouterr().err


def test_index_is_newest_first_and_capped_at_200(tmp_path):
    ids = []
    for i in range(205):
        rid = runs.make_run_id(T0 + timedelta(seconds=i))
        ids.append(rid)
        assert runs.write_run(tmp_path, make(run_id=rid))
    idx = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert len(idx) == runs.INDEX_MAX == 200
    assert [e["run_id"] for e in idx] == ids[::-1][:200]                    # 최신순, 가장 오래된 5개는 목록에서 빠짐
    assert set(idx[0]) == {"run_id", "created_at", "kind", "title", "model", "status", "exportable"}
    assert len(list((tmp_path / "runs").glob("r-*.json"))) == 205            # 기록 파일 자체는 지우지 않는다


def test_record_over_2mb_is_not_written(tmp_path, capsys):
    big = [{"type": "chunk", "n": 1, "text": "가" * (3 * 1024 * 1024), "gap_ms": 1.0, "since_start_ms": 1.0}]
    assert runs.write_run(tmp_path, make(chunks=big, done={**DONE, "joined": "가"})) is False
    assert not (tmp_path / "runs").exists() or list((tmp_path / "runs").iterdir()) == []
    assert not (tmp_path / "index.json").exists()
    assert "크기" in capsys.readouterr().err


def test_invalid_record_is_not_written(tmp_path, capsys):
    rec = make()
    del rec["settings"]
    assert runs.write_run(tmp_path, rec) is False
    assert not (tmp_path / "index.json").exists() and "검증 실패" in capsys.readouterr().err


def test_existing_file_is_not_overwritten(tmp_path):
    rec = make()
    assert runs.write_run(tmp_path, rec) is True
    assert runs.write_run(tmp_path, make(status="error", done=None, chunks=[], error="x")) is False
    assert json.loads((tmp_path / "runs" / f"{rec['run_id']}.json").read_text(encoding="utf-8"))["status"] == "ok"


# ---- validate_bundle
def test_valid_record_passes():
    assert runs.validate_bundle(make()) == []
    assert runs.validate_bundle(make(chunks=[], done=None, status="error", error="boom")) == []
    assert runs.validate_bundle(make(done=None, status="aborted")) == []


def test_seed_key_is_omitted_when_not_given_and_present_when_given():
    assert "seed" not in make()["settings"]
    rec = runs.build_playground_record(
        run_id="r-20261028-064512-ab12", created_at=runs.utc_iso(T0), prompt="안녕", model="m",
        options={**OPTIONS, "seed": 42}, ollama_version=None, tokenizer=TOKENIZER, tok_result=TOK,
        tok_ms=1.0, chunks=CHUNKS, done=DONE, status="ok")
    assert rec["settings"]["seed"] == 42 and runs.validate_bundle(rec) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r.pop("settings"), "필수 필드 없음: settings"),
    (lambda r: r.pop("stages"), "필수 필드 없음: stages"),
    (lambda r: r.update(status="done"), "status는"),
    (lambda r: r["stages"][0].update(status="fine"), "input: status는"),
    (lambda r: r["stages"][0]["evidence"].update({"prompt": "guess"}), "모르는 evidence 등급"),
    (lambda r: r["stages"][1].update(id="input"), "단계 id 중복: input"),
    (lambda r: r.update(run_id="R-1"), "run_id 모양"),
    (lambda r: r.update(created_at="2026-10-28 06:45"), "created_at 모양"),
    (lambda r: r.update(schema_version=2), "schema_version"),
    (lambda r: r.update(kind="other"), "kind는"),
    (lambda r: r.update(exportable="no"), "exportable"),
    (lambda r: r["stages"][0]["evidence"].pop("*"), "기본값 '*'"),
    (lambda r: r["stages"][0]["output"].pop("prompt"), "output에 prompt가 없음"),
    (lambda r: r["stages"][2].update(duration_ms="fast"), "duration_ms"),
])
def test_validate_bundle_reports_problems(mutate, expect):
    rec = copy.deepcopy(make())
    mutate(rec)
    problems = runs.validate_bundle(rec)
    assert problems and any(expect in p for p in problems), problems


def test_input_grade_is_allowed_and_unknown_grades_are_still_rejected():
    assert runs.GRADES == ("measured", "computed", "illustrative", "unverified", "input")
    rec = copy.deepcopy(make())
    rec["stages"][0]["evidence"]["prompt"] = "input"                       # 새 등급은 검증을 통과한다
    assert runs.validate_bundle(rec) == []
    rec["stages"][0]["evidence"]["prompt"] = "guess"                       # 모르는 등급은 계속 거부된다
    assert any("모르는 evidence 등급" in p for p in runs.validate_bundle(rec))


def test_old_record_with_computed_input_stage_still_validates():
    rec = copy.deepcopy(make())
    # 이전 실행 기록(입력 단계를 계산으로 적은 것)
    rec["stages"][0]["evidence"] = {"*": "computed"}
    assert runs.validate_bundle(rec) == [] and rec["schema_version"] == 1


def test_input_stage_marks_user_values_as_input_and_counts_as_computed():
    rec = make()
    inp = next(s for s in rec["stages"] if s["id"] == "input")
    assert inp["evidence"] == {"*": "input", "prompt_chars": "computed", "prompt_bytes": "computed"}
    assert rec["schema_version"] == 1


def test_validate_rejects_non_dict():
    assert runs.validate_bundle([]) == ["기록이 객체가 아님"]


# ---- 오류·중단: 뒤 단계는 skipped
def _status(rec):
    return {s["id"]: s["status"] for s in rec["stages"]}


def test_error_before_any_chunk_marks_first_ollama_stage_and_skips_the_rest():
    rec = make(chunks=[], done=None, status="error", error="연결 실패")
    assert rec["status"] == "error" and rec["error"] == "연결 실패"
    assert _status(rec) == {"input": "ok", "tokenize": "ok", "load": "error", "prefill": "skipped",
                            "decode": "skipped", "restore": "skipped"}
    load_notes = next(s for s in rec["stages"] if s["id"] == "load")["notes"]
    assert "연결 실패" in " ".join(load_notes)
    assert ("첫 조각이 오기 전에 연결이 끊겨 올리기와 읽기 중 어느 단계에서 멈췄는지 알 수 없다. "
            "올리기 단계에 기록한다.") in load_notes


def test_abort_after_some_chunks_keeps_chunks_and_skips_restore():
    rec = make(done=None, status="aborted")
    assert rec["status"] == "aborted" and rec["error"]
    assert _status(rec) == {"input": "ok", "tokenize": "ok", "load": "ok", "prefill": "ok",
                            "decode": "error", "restore": "skipped"}
    dec = next(s for s in rec["stages"] if s["id"] == "decode")
    assert dec["output"]["chunk_count"] == 2 and [c["text"] for c in dec["output"]["chunks"]] == ["안", "녕"]
    assert any("중단" in n for n in dec["notes"])
    # 값이 없는 단계는 미확인
    assert all(s["evidence"]["*"] == "unverified" for s in rec["stages"] if s["id"] in ("load", "prefill"))


def test_stages_before_the_failure_are_kept_as_is():
    ok, bad = make(), make(chunks=[], done=None, status="error", error="x")
    for sid in ("input", "tokenize"):
        a, b = (next(s for s in r["stages"] if s["id"] == sid) for r in (ok, bad))
        assert a["output"] == b["output"] and a["status"] == b["status"] == "ok"


def test_ok_record_has_six_stages_with_measured_and_computed_evidence():
    rec = make()
    assert [s["id"] for s in rec["stages"]] == list(runs.PLAYGROUND_STAGES)
    dec = next(s for s in rec["stages"] if s["id"] == "decode")
    assert dec["evidence"] == {"*": "measured", "tok_s": "computed"} and dec["output"]["tok_s"] == 1.333
    assert dec["output"]["first_chunk_ms"] == 20.0 and dec["duration_ms"] == 1500.0
    pre = next(s for s in rec["stages"] if s["id"] == "prefill")
    assert pre["output"]["count_diff"] == 29 and pre["output"]["prompt_eval_cached_count"] == 0
    assert rec["exportable"] is False and rec["redaction"]["applied"] is False and rec["title"] == "안녕"
