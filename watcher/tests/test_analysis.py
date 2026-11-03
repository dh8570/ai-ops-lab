import json
from pathlib import Path

import analysis
from analysis import (
    ANALYSIS_SCHEMA,
    SYSTEM_PROMPT_V1,
    ParsedAnalysis,
    build_evidence,
    build_messages,
    estimate_tokens,
    exception_kind,
    load_runbooks,
    mask,
    normalize_path,
    parse_answer,
    render_evidence,
    render_slack_text,
)
from state import Incident
from stats import WindowStats, summarize

NOW = 1_800_000_000.0
RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "runbooks"


def _incident(id=12, severity="critical", rules=("R1",), opened_ago_sec=180):
    from datetime import UTC, datetime
    opened = datetime.fromtimestamp(NOW - opened_ago_sec, UTC).isoformat(timespec="seconds")
    return Incident(id, "visit-counter", "FIRING", severity, list(rules), opened, None, 1, 0)


def _rec(path="/", status=200, latency=20.0, error=None, **extra):
    return {"ts": "2026-11-02T01:00:00.000+00:00", "service": "visit-counter", "request_id": "abc123def456",
            "method": "GET", "path": path, "status": status, "latency_ms": latency, "error": error, **extra}


def _sample_records():
    recs = [_rec("/error", 500, 5.0, "RuntimeError: simulated failure (/error)") for _ in range(478)]
    recs += [_rec("/", 200, 22.0) for _ in range(42)]
    return recs


_DEFAULT = object()


def _evidence(records=_DEFAULT, **kw):
    recs = _sample_records() if records is _DEFAULT else records
    stats = summarize(recs or [], 60, ("/healthz",))
    args = dict(incident=_incident(), window_stats=stats, records=recs, baseline=300.0,
                probe={"ok": True, "detail": "HTTP 200", "latency_ms": 3.0, "consecutive_failures": 0}, now_ts=NOW)
    args.update(kw)
    return build_evidence(**args)


# ---------------------------------------------------------------- normalize_path

def test_normalize_path_strips_query_and_fragment():
    assert normalize_path("/slow?delay=3&token=abc123") == "/slow"
    assert normalize_path("/a/b#frag?x") == "/a/b"


def test_normalize_path_replaces_id_like_segments():
    assert normalize_path("/users/12345/orders") == "/users/{id}/orders"
    assert normalize_path("/f/deadbeef01") == "/f/{id}"
    assert normalize_path("/f/123e4567-e89b-12d3-a456-426614174000") == "/f/{id}"
    assert normalize_path("/f/" + "a" * 32) == "/f/{id}"
    assert normalize_path("/host/10.0.0.5") == "/host/{id}"
    assert normalize_path("/api/v1/items") == "/api/v1/items"        # v1, items는 id가 아니다


def test_normalize_path_length_cap():
    out = normalize_path("/" + "/".join(["abcdefg"] * 40))
    assert len(out) <= 80
    assert out.startswith("/abcdefg")


def test_normalize_path_odd_characters_become_other():
    for bad in ("/token=abc123", "/user@example.com", "/a b", "/한글", "no-slash", "", None, "/a\n"):
        assert normalize_path(bad) == "/기타"


# ---------------------------------------------------------------- exception_kind

def test_exception_kind_drops_message():
    kind = exception_kind("RuntimeError: connect to 10.0.0.5 failed password=abc")
    assert kind == "RuntimeError"
    assert "10.0.0.5" not in kind and "password" not in kind


def test_exception_kind_dotted_name_ok_and_odd_head_is_other():
    assert exception_kind("psycopg.OperationalError: x") == "psycopg.OperationalError"
    for bad in ("10.0.0.5: refused", "user@example.com: x", "password=abc: x", "no colon here", "x" * 61 + ": m", None):
        assert exception_kind(bad) == "기타"


# ---------------------------------------------------------------- 민감 문자열

SECRETS = ["token=abc123", "abc123", "10.0.0.5", "user@example.com", "example.com", "password=hunter2", "hunter2",
           "Bearer xyz", "xyz"]


def test_secrets_never_reach_evidence_or_messages():
    dirty = [
        _rec("/x?token=abc123", 500, error="RuntimeError: login user@example.com password=hunter2"),
        _rec("/10.0.0.5/x", 500, error="Authorization: Bearer xyz"),
        _rec("/token=abc123", 200, error="token=abc123"),
        _rec("/user@example.com", 200, error="10.0.0.5: refused"),
        _rec("/password=hunter2", 200, error="user@example.com: x"),
        _rec("/Authorization: Bearer xyz", 500, error="RuntimeError: Authorization: Bearer xyz"),
        _rec("/ok", 200, request_id="token=abc123", method="password=hunter2", note="user@example.com 10.0.0.5"),
    ]
    ev = _evidence(dirty, probe={"consecutive_failures": 2, "detail": "token=abc123"})
    runbooks = load_runbooks(RUNBOOK_DIR)
    texts = [json.dumps(ev, ensure_ascii=False)] + [m["content"] for m in build_messages(ev, runbooks)]
    for text in texts:
        for piece in SECRETS:
            assert piece not in text, piece


# ---------------------------------------------------------------- build_evidence

def test_build_evidence_shape_and_key_order():
    ev = _evidence()
    assert list(ev) == ["incident", "window", "baseline", "top_error_kinds", "top_paths", "probe",
                        "records_available"]
    assert ev["incident"] == {"id": 12, "severity": "critical", "opened_min_ago": 3,
                              "fired_rules": [{"rule": "R1", "detail": "error_rate 0.92, 5xx 478/520"}]}
    assert ev["window"] == {"sec": 60, "requests": 520, "errors_5xx": 478, "error_rate": 0.919,
                            "p95_ms": 22.0, "rpm": 520.0}
    assert ev["baseline"] == {"rpm": 300.0}
    assert ev["top_error_kinds"] == [{"kind": "RuntimeError", "count": 478}]
    assert ev["top_paths"][0] == {"path": "/error", "count": 478, "status_counts": {"2xx": 0, "4xx": 0, "5xx": 478}}
    assert ev["probe"] == {"consecutive_failures": 0, "last_reason_code": "HTTP 200"}
    assert ev["records_available"] is True


def test_build_evidence_excludes_healthz_and_caps_counts():
    recs = [_rec(f"/p{i}", 200) for i in range(9)] + [_rec("/healthz", 200) for _ in range(50)]
    recs += [_rec("/e", 500, error=f"E{i}Error: m") for i in range(8)]
    ev = _evidence(recs)
    assert len(ev["top_paths"]) == 5
    assert len(ev["top_error_kinds"]) == 5
    assert all(p["path"] != "/healthz" for p in ev["top_paths"])


def test_build_evidence_string_length_caps():
    recs = [_rec("/" + "a" * 500, 200, error="E" * 500 + ": m")]
    ev = _evidence(recs)
    assert len(ev["top_paths"][0]["path"]) <= 80
    assert ev["top_error_kinds"][0]["kind"] == "기타"          # 60자 초과 종류는 기타


def test_build_evidence_records_none():
    ev = _evidence(records=None, window_stats=WindowStats(0, 0, 0.0, 0.0))
    assert ev["top_paths"] == [] and ev["top_error_kinds"] == []
    assert ev["records_available"] is False
    assert "[오류 유형]" not in render_evidence(ev)


def test_build_evidence_missing_baseline_and_probe():
    ev = _evidence(baseline=None, probe=None)
    assert ev["baseline"] is None and ev["probe"] is None
    text = render_evidence(ev)
    assert "[기준선]" not in text and "[프로브]" not in text


def test_build_evidence_r3_r4_details_and_prober_object():
    class P:
        consecutive_failures = 2
        last_detail = "timeout"
    ev = _evidence(incident=_incident(rules=("R4", "R3", "bad rule")), probe=P(),
                   window_stats=WindowStats(30, 0, 10.0, 30.0))
    assert ev["incident"]["fired_rules"] == [{"rule": "R3", "detail": "rpm 30, 기준선 300"},
                                            {"rule": "R4", "detail": "연속 실패 2회"}]
    assert ev["probe"] == {"consecutive_failures": 2, "last_reason_code": "timeout"}


def test_build_evidence_is_deterministic_bytes():
    a = json.dumps(_evidence(), ensure_ascii=False)
    b = json.dumps(_evidence(), ensure_ascii=False)
    assert a == b


def test_build_evidence_shrinks_when_estimate_exceeds_budget():
    recs = [_rec(f"/p{i}", 200) for i in range(8)] + [_rec("/e", 500, error=f"E{i}Error: m") for i in range(7)]
    assert len(_evidence(recs)["top_paths"]) == 5
    big = _evidence(recs, runbooks_text="가" * 2700)
    assert len(big["top_paths"]) == 3 and len(big["top_error_kinds"]) == 2


# ---------------------------------------------------------------- build_messages / load_runbooks

def test_system_text_identical_across_incidents_and_user_differs():
    runbooks = load_runbooks(RUNBOOK_DIR)
    m1 = build_messages(_evidence(), runbooks)
    other = _evidence([_rec("/slow", 200, 3100.0) for _ in range(30)], incident=_incident(id=13, rules=("R2",)))
    m2 = build_messages(other, runbooks)
    assert m1[0] == m2[0]
    assert m1[0]["role"] == "system" and m1[1]["role"] == "user"
    assert m1[0]["content"].startswith(SYSTEM_PROMPT_V1)
    assert m1[1]["content"] != m2[1]["content"]


def test_build_messages_goes_through_mask(monkeypatch):
    calls = []

    def fake(text):
        calls.append(text)
        return f"<{text}>"

    monkeypatch.setattr(analysis, "mask", fake)
    msgs = build_messages(_evidence(), "RB")
    assert len(calls) == 1                                    # user 글에만 (system은 고정 글이라 마스킹하지 않는다)
    assert calls[0].startswith("[장애 요약]")
    assert msgs[0]["content"].startswith(SYSTEM_PROMPT_V1)
    assert msgs[1]["content"].startswith("<") and msgs[1]["content"].endswith(">")


def test_mask_is_identity_for_now():
    assert mask("10.0.0.5 user@example.com") == "10.0.0.5 user@example.com"


def test_load_runbooks_sorted_and_skips_draft_line(tmp_path):
    (tmp_path / "02_b.md").write_text("초안: PM 검토 필요\n# 제목B\n본문B\n", encoding="utf-8")
    (tmp_path / "01_a.md").write_text("초안: PM 검토 필요\n제목A\n본문A1\n본문A2\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("무시", encoding="utf-8")
    assert load_runbooks(tmp_path) == "[런북] 제목A\n본문A1\n본문A2\n\n[런북] 제목B\n본문B"
    assert load_runbooks(tmp_path / "missing") == ""


def test_repo_runbooks_are_short_and_draft_marked():
    files = sorted(RUNBOOK_DIR.glob("*.md"))
    assert [f.name for f in files] == ["01_5xx_spike.md", "02_latency.md", "03_app_down.md"]
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 15
        assert lines[0] == "초안: PM 검토 필요"
    assert "초안" not in load_runbooks(RUNBOOK_DIR)


def test_estimated_tokens_fit_in_context():
    msgs = build_messages(_evidence(), load_runbooks(RUNBOOK_DIR))
    total = sum(estimate_tokens(m["content"]) for m in msgs)
    assert total <= analysis.PROMPT_TOKEN_BUDGET
    assert total + analysis.MAX_OUTPUT_TOKENS <= analysis.NUM_CTX


def test_build_messages_shrinks_a_wide_evidence_when_runbooks_are_big():
    recs = [_rec(f"/p{i}", 200) for i in range(8)] + [_rec("/e", 500, error=f"E{i}Error: m") for i in range(7)]
    ev = _evidence(recs)                                    # 런북 없이 만들었으니 5, 5
    text = build_messages(ev, "가" * 2700)[1]["content"]
    assert text.count("Err") == 2 and "/p3 " not in text


# ---------------------------------------------------------------- parse_answer

def _ans(**over):
    d = {"summary": "에러율이 높다.", "causes": ["원인 하나", "원인 둘"], "checks": ["확인 하나", "확인 둘"]}
    d.update(over)
    return json.dumps(d, ensure_ascii=False)


def test_parse_answer_ok():
    p = parse_answer(_ans())
    assert p.ok and p.reason == "" and p.flags == []
    assert p.summary == "에러율이 높다." and len(p.causes) == 2 and len(p.checks) == 2


def test_parse_answer_bad_json_and_non_object():
    for text in ("{not json", "", None, "[1, 2]", '"x"'):
        p = parse_answer(text)
        assert not p.ok and p.reason == "bad_output"


def test_parse_answer_missing_or_wrong_type_fields():
    assert not parse_answer(json.dumps({"summary": "s", "causes": ["a", "b"]})).ok
    assert not parse_answer(_ans(summary=3)).ok
    assert not parse_answer(_ans(causes="a, b")).ok
    assert not parse_answer(_ans(checks=["a", 2])).ok


def test_parse_answer_item_counts():
    assert not parse_answer(_ans(causes=["하나만"])).ok                       # 미달은 거부
    assert not parse_answer(_ans(checks=[])).ok
    p = parse_answer(_ans(causes=["a", "b", "c", "d", "e"]))                   # 초과는 잘라 냄
    assert p.ok and p.causes == ["a", "b", "c"] and "trimmed" in p.flags


def test_parse_answer_trims_long_text():
    p = parse_answer(_ans(summary="가" * 400, checks=["나" * 250, "x"]))
    assert p.ok and "trimmed" in p.flags
    assert len(p.summary) == 300 and len(p.checks[0]) == 200


def test_parse_answer_foreign_script_flag():
    for bad in ("원인은 数据库 문제", "これは問題", "Привет", "สวัสดี", "مرحبا"):
        p = parse_answer(_ans(causes=[bad, "b"]))
        assert p.ok and "foreign_script" in p.flags, bad
    assert "foreign_script" not in parse_answer(_ans(summary="DB(PostgreSQL) 연결 실패 · 5xx 478/520, p95=22ms!")).flags


def test_parse_answer_unknown_marker_and_empty_field():
    p = parse_answer(_ans(causes=["알 수 없음", "b"]))
    assert p.ok and p.flags == ["unknown_marker"]
    p = parse_answer(_ans(checks=["  ", "b"]))
    assert p.ok and p.flags == ["empty_field"]


def test_schema_constants():
    assert ANALYSIS_SCHEMA["required"] == ["summary", "causes", "checks"]
    assert ANALYSIS_SCHEMA["properties"]["causes"]["minItems"] == 2


# ---------------------------------------------------------------- render_slack_text

def test_render_slack_text_escape_numbering_and_last_line():
    parsed = ParsedAnalysis(ok=True, summary="a<b & c>d", causes=["x & y", "<z>"], checks=["c1", "c2", "c3"])
    out = render_slack_text(parsed, "qwen2.5:7b", 12.34, 800, 120)
    lines = out.split("\n")
    assert lines[0] == "a&lt;b &amp; c&gt;d"
    assert "1. x &amp; y" in lines and "2. &lt;z&gt;" in lines
    assert "1. c1" in lines and "3. c3" in lines
    assert lines[-1] == "모델 qwen2.5:7b · 12.3초 · 입력 800tok / 출력 120tok · AI 생성, 검증 전"
    assert "<" not in out and ">" not in out
