"""스트림 파서: 가짜 NDJSON(조각 여러 개 + 마지막 통계)에서 조각·간격·통계가 맞게 나오는가."""
import json

import streamparse as S


def _lines(*objs):
    return [json.dumps(o, ensure_ascii=False) + "\n" for o in objs]


def _clock(*times):
    it = iter(times)
    return lambda: next(it)


FINAL = {"model": "m", "response": "", "done": True, "done_reason": "stop", "context": [1, 2, 3],
         "total_duration": 3_000_000_000, "load_duration": 1_000_000_000, "prompt_eval_count": 31,
         "prompt_eval_cached_count": 0, "prompt_eval_duration": 500_000_000, "eval_count": 3,
         "eval_duration": 1_500_000_000}


def test_chunks_gaps_and_final_stats():
    lines = _lines({"response": "안", "done": False}, {"response": "녕", "done": False},
                   {"response": "하세요", "done": False}, FINAL)
    # t0=0 이고, 줄이 0.25 / 0.5 / 1.0 / 1.25초에 도착했다고 가정
    events = list(S.parse_stream(lines, _clock(0.25, 0.5, 1.0, 1.25), 0.0))
    chunks = [e for e in events if e["type"] == "chunk"]
    assert [(c["n"], c["text"]) for c in chunks] == [(1, "안"), (2, "녕"), (3, "하세요")]
    assert [c["gap_ms"] for c in chunks] == [250.0, 250.0, 500.0]            # 첫 조각의 간격은 t0 기준
    assert [c["since_start_ms"] for c in chunks] == [250.0, 500.0, 1000.0]
    done = events[-1]
    assert done["type"] == "done"
    assert done["chunks"] == 3 and done["joined"] == "안녕하세요"
    assert done["final_response"] == ""                                        # 마지막 줄 response 필드 그대로
    assert done["stats"]["eval_count"] == 3 and done["stats"]["load_duration"] == 1_000_000_000
    assert done["stats"]["done_reason"] == "stop"
    assert "context" not in done["stats"]
    assert done["since_start_ms"] == 1250.0


def test_accepts_bytes_and_skips_blank_lines():
    lines = [b"\n", json.dumps({"response": "a", "done": False}).encode() + b"\n", b"   \n",
             json.dumps(FINAL).encode() + b"\n"]
    events = list(S.parse_stream(lines, _clock(0.1, 0.2), 0.0))
    assert [e["type"] for e in events] == ["chunk", "done"]


def test_done_line_with_text_is_counted_as_a_chunk():
    final = {**FINAL, "response": "끝"}
    events = list(S.parse_stream(_lines({"response": "가", "done": False}, final), _clock(0.1, 0.2), 0.0))
    assert [e["type"] for e in events] == ["chunk", "chunk", "done"]
    assert events[-1]["joined"] == "가끝" and events[-1]["chunks"] == 2


def test_error_line_stops_the_stream():
    events = list(S.parse_stream(_lines({"response": "a", "done": False}, {"error": "model not found"}, FINAL),
                                 _clock(0.1, 0.2, 0.3), 0.0))
    assert [e["type"] for e in events] == ["chunk", "error"]
    assert events[-1]["message"] == "model not found"


def test_bad_json_is_reported_not_raised():
    events = list(S.parse_stream(["{not json\n"], _clock(0.1), 0.0))
    assert events[0]["type"] == "error"


def test_stream_ending_without_done_is_an_error():
    events = list(S.parse_stream(_lines({"response": "a", "done": False}), _clock(0.1), 0.0))
    assert events[-1]["type"] == "error"
    assert events[-1]["chunks"] == 1 and events[-1]["joined"] == "a"
