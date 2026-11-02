import copy
import json
import re
import threading
from datetime import UTC, datetime

import pytest
from fake_ollama import FakeOllama

import llm_trace
from llm_client import LlmOptions, LlmResult, chat
from llm_trace import append_run, build_run_record, llm_call_stage, make_stage, validate_record

OPTS = LlmOptions(temperature=0, seed=42, top_k=40, top_p=0.9, repeat_penalty=1.0, num_predict=512, num_ctx=4096)
NOW = datetime(2026, 11, 2, 3, 4, 5, 678901, tzinfo=UTC)
SECRET = "프롬프트-본문-마커-9c1d"


def _stage(id="evidence", **kw):
    return make_stage(id, kw.get("status", "ok"), kw.get("duration_ms"), kw.get("evidence", {"*": "measured"}),
                      kw.get("output", {}), kw.get("notes", ()))


def _rec(stages=None, **kw):
    args = dict(incident_id=7, trigger="R1", title="incident #7", model="qwen2.5:7b", prompt_version="p1",
                options=OPTS, stages=stages if stages is not None else [_stage()], status="ok", error=None,
                ollama_version="0.40.2", now=NOW, rid="r-20261102-030405-ab12")
    args.update(kw)
    return build_run_record(**args)


def test_good_record_passes_and_has_v1_shape():
    rec = _rec()
    assert validate_record(rec) == []
    assert rec["schema_version"] == 1 and rec["kind"] == "incident" and rec["exportable"] is False
    assert rec["created_at"] == "2026-11-02T03:04:05.678Z"
    assert rec["run_id"] == "r-20261102-030405-ab12"
    assert rec["redaction"]["applied"] is False
    assert rec["environment"] == {"ollama_version": "0.40.2", "model": "qwen2.5:7b", "prompt_version": "p1"}
    assert rec["settings"] == OPTS.as_dict() and len(rec["settings"]) == 7
    json.dumps(rec)


def test_generated_run_id_matches_pattern():
    ids = {_rec(rid=None)["run_id"] for _ in range(5)}
    assert all(re.fullmatch(r"r-20261102-030405-[0-9a-f]{4}", i) for i in ids)


def test_non_utc_now_is_converted_to_utc():
    from datetime import timedelta, timezone
    kst = datetime(2026, 11, 2, 12, 4, 5, tzinfo=timezone(timedelta(hours=9)))
    rec = _rec(now=kst, rid=None)
    assert rec["created_at"] == "2026-11-02T03:04:05.000Z" and rec["run_id"].startswith("r-20261102-030405-")


@pytest.mark.parametrize("run_id", ["", "r-1", "r-20261102-030405-AB12", "r-20261102-030405-ab123",
                                    "x-20261102-030405-ab12", "r-20261102-030405-ab12\n", "../r-20261102-030405-ab12"])
def test_bad_run_id_rejected(run_id):
    assert any("run_id" in p for p in validate_record(_rec(rid=run_id)))


def test_unknown_grade_stage_and_status_rejected():
    assert any("등급" in p for p in validate_record(_rec([_stage(evidence={"*": "guess"})])))
    assert any("등급" in p for p in validate_record(_rec([_stage(evidence={"*": "measured", "x": "unknown"})])))
    assert any("단계" in p for p in validate_record(_rec([_stage(id="mystery")])))
    assert any("status" in p for p in validate_record(_rec([_stage(status="done")])))
    assert any("status" in p for p in validate_record(_rec(status="finished")))


def test_all_five_grades_and_stage_ids_accepted():
    grades = {"*": "measured", "a": "computed", "b": "illustrative", "c": "unverified", "d": "input"}
    stages = [_stage(id=i, evidence=grades) for i in ("evidence", "prompt", "llm_call", "parse", "notify")]
    assert validate_record(_rec(stages)) == []


def test_structure_problems_rejected():
    assert any("\"*\"" in p for p in validate_record(_rec([_stage(evidence={"x": "measured"})])))
    assert any("중복" in p for p in validate_record(_rec([_stage(), _stage()])))
    assert any("duration_ms" in p for p in validate_record(_rec([_stage(duration_ms=-1)])))
    assert any("duration_ms" in p for p in validate_record(_rec([_stage(duration_ms=float("nan"))])))
    assert any("notes" in p for p in validate_record(_rec([_stage(notes=[1])])))
    assert any("exportable" in p for p in validate_record({**_rec(), "exportable": True}))
    assert any("kind" in p for p in validate_record({**_rec(), "kind": "playground"}))
    assert any("error" in p for p in validate_record(_rec(status="error", error=None)))
    assert any("error" in p for p in validate_record(_rec(status="ok", error="x")))
    assert validate_record("not a dict") and validate_record({})


def test_missing_or_extra_setting_rejected():
    rec = _rec()
    del rec["settings"]["seed"]
    assert any("settings" in p for p in validate_record(rec))
    rec = _rec()
    rec["settings"]["mirostat"] = 1
    assert any("settings" in p for p in validate_record(rec))
    rec = _rec()
    rec["settings"]["top_p"] = None
    assert any("settings" in p for p in validate_record(rec))


def test_record_over_2mb_rejected():
    big = _stage(output={"text": "가" * (2 * 1024 * 1024)})
    assert any("상한" in p for p in validate_record(_rec([big])))


def test_non_serializable_rejected():
    assert any("직렬화" in p for p in validate_record(_rec([_stage(output={"x": object()})])))


def test_llm_call_stage_grades_for_a_real_result():
    with FakeOllama("ok") as fake:
        r = chat(fake.url, "qwen2.5:7b", [{"role": "user", "content": "x"}], OPTS, total_timeout=5)
    st = llm_call_stage(r)
    assert st["id"] == "llm_call" and st["status"] == "ok" and st["duration_ms"] == r.wall_ms
    ev = st["evidence"]
    assert ev["*"] == "measured"
    assert ev["options"] == ev["model"] == ev["keep_alive"] == "input"
    assert ev["tok_s"] == "computed" and ev["outside_ollama_ms"] == "computed"
    assert st["output"]["tok_s"] == 40.0 and st["output"]["prompt_eval_cached_count"] == 32
    assert st["output"]["options"] == OPTS.as_dict()
    assert validate_record(_rec([st])) == []


def test_llm_call_stage_marks_missing_values_unverified():
    st = llm_call_stage(LlmResult(ok=False, reason="timeout", wall_ms=30001.0,
                                  request={"model": "m", "keep_alive": "24h", "options": OPTS.as_dict()}))
    assert st["status"] == "error" and st["notes"] == ["실패: timeout"]
    assert st["evidence"]["load_ms"] == "unverified" and st["evidence"]["tok_s"] == "unverified"
    assert st["evidence"].get("wall_ms", st["evidence"]["*"]) == "measured"   # 키가 없으면 "*"(기본 등급)를 따른다
    assert validate_record(_rec([st], status="error", error="timeout")) == []


def test_prompt_body_never_reaches_the_record():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": SECRET}]
    with FakeOllama("ok") as fake:
        r = chat(fake.url, "qwen2.5:7b", msgs, OPTS, total_timeout=5)
    rec = _rec([llm_call_stage(r)])
    assert validate_record(rec) == []
    assert SECRET not in json.dumps(rec, ensure_ascii=False)


def test_append_run_writes_one_json_line_per_record(tmp_path):
    path = tmp_path / "llm_runs.jsonl"
    r1, r2 = _rec(rid="r-20261102-030405-0001", title="가"), _rec(rid="r-20261102-030405-0002", title="나\n줄바꿈")
    assert append_run(path, r1) is True and append_run(path, r2) is True
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "" and len(lines) == 3
    assert [json.loads(x)["run_id"] for x in lines[:2]] == ["r-20261102-030405-0001", "r-20261102-030405-0002"]
    assert json.loads(lines[1])["title"] == "나\n줄바꿈"


def test_append_run_creates_missing_directory(tmp_path):
    path = tmp_path / "sub" / "llm_runs.jsonl"
    assert append_run(path, _rec()) is True and path.exists()


def test_append_run_rotates_when_file_is_over_limit(tmp_path):
    path = tmp_path / "llm_runs.jsonl"
    rotated = tmp_path / "llm_runs.jsonl.1"
    assert append_run(path, _rec(rid="r-20261102-030405-0001"), max_bytes=10)
    assert append_run(path, _rec(rid="r-20261102-030405-0002"), max_bytes=10)   # 첫 파일이 10바이트를 넘었으므로 돌린다
    assert "0001" in rotated.read_text(encoding="utf-8") and "0002" in path.read_text(encoding="utf-8")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert append_run(path, _rec(rid="r-20261102-030405-0003"), max_bytes=10)   # 기존 .1은 덮어쓴다
    assert "0002" in rotated.read_text(encoding="utf-8") and "0001" not in rotated.read_text(encoding="utf-8")
    assert append_run(path, _rec(rid="r-20261102-030405-0004"))                 # 상한 안이면 그냥 이어 쓴다
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_append_run_default_limit_is_20mb():
    assert llm_trace.MAX_FILE_BYTES == 20 * 1024 * 1024 and llm_trace.MAX_RECORD_BYTES == 2 * 1024 * 1024


def test_append_run_ignores_write_failure(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert append_run(blocker / "llm_runs.jsonl", _rec()) is False         # 디렉터리를 만들 수 없다
    assert "기록 실패" in capsys.readouterr().err


def test_append_run_skips_invalid_record(tmp_path, capsys):
    path = tmp_path / "llm_runs.jsonl"
    bad = copy.deepcopy(_rec())
    bad["stages"][0]["id"] = "mystery"
    assert append_run(path, bad) is False and not path.exists()
    assert "검증 실패" in capsys.readouterr().err
    assert append_run(path, "not a dict") is False


def test_append_run_is_safe_under_threads(tmp_path):
    path = tmp_path / "llm_runs.jsonl"
    recs = [_rec(rid=f"r-20261102-030405-{i:04x}", stages=[_stage(output={"pad": "x" * 20000})]) for i in range(20)]
    threads = [threading.Thread(target=append_run, args=(path, r)) for r in recs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20 and len({json.loads(x)["run_id"] for x in lines}) == 20
