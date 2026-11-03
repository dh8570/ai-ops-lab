"""A1-T3 작업 0: analysis.py 보완 5건(a~e)."""
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

import analysis
from analysis import (
    PREVIOUS_WINDOW_DETAIL,
    SYSTEM_PROMPT_V1,
    build_evidence,
    build_messages,
    exception_kind,
    load_runbooks,
    render_evidence,
)
from state import Incident
from stats import WindowStats

NOW = 1_800_000_000.0
RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "runbooks"
SYSTEM_PROMPT_V1_SHA256 = "62aa479148e8ec8bd5ae74fc3534e9a5e31b0b1b5539e02468f0afb12b63f9e8"   # T2 브랜치의 값


def _evidence(rules=("R1", "R3"), **kw):
    opened = datetime.fromtimestamp(NOW - 300, UTC).isoformat(timespec="seconds")
    inc = Incident(5, "visit-counter", "FIRING", "critical", list(rules), opened, None, 2, 0)
    args = dict(incident=inc, window_stats=WindowStats(100, 40, 2500.0, 100.0), records=[], baseline=300.0,
                probe={"consecutive_failures": 2, "detail": "timeout"}, now_ts=NOW)
    args.update(kw)
    return build_evidence(**args)


# ---------------------------------------------------------------- a) exception_kind

@pytest.mark.parametrize("head", ["RuntimeError", "ValueException", "UserWarning", "SystemExit", "KeyboardInterrupt",
                                  "ReadTimeout", "AssertFailure", "psycopg.OperationalError"])
def test_exception_kind_keeps_exception_like_names(head):
    assert exception_kind(f"{head}: secret message") == head


@pytest.mark.parametrize("text", ["Authorization: Bearer x", "Cookie: session=abc", "Host: example.com",
                                  "token: abc", "Errors: x", "socket.timeout: x", "ErrorCode: 7"])
def test_exception_kind_header_like_names_are_other(text):
    assert exception_kind(text) == "기타"


# ---------------------------------------------------------------- b) system 글의 [런북] 제목 중복 제거

def test_system_prompt_v1_is_unchanged():
    assert hashlib.sha256(SYSTEM_PROMPT_V1.encode("utf-8")).hexdigest() == SYSTEM_PROMPT_V1_SHA256


def test_system_text_has_one_runbook_heading_per_block():
    runbooks = load_runbooks(RUNBOOK_DIR)
    system = build_messages(_evidence(), runbooks)[0]["content"]
    assert system == SYSTEM_PROMPT_V1 + "\n\n" + runbooks
    assert system.count("[런북]") == SYSTEM_PROMPT_V1.count("[런북]") + 3      # 지시문 안의 언급 + 런북 블록 3개
    assert "[런북]\n[런북]" not in system


def test_system_text_without_runbooks_is_prompt_only():
    assert build_messages(_evidence(), "")[0]["content"] == SYSTEM_PROMPT_V1


# ---------------------------------------------------------------- c) mask는 user 글에만

def test_mask_applies_only_to_user(monkeypatch):
    monkeypatch.setattr(analysis, "mask", lambda t: "MASKED:" + t)
    runbooks = load_runbooks(RUNBOOK_DIR)
    system, user = (m["content"] for m in build_messages(_evidence(), runbooks))
    assert system == SYSTEM_PROMPT_V1 + "\n\n" + runbooks
    assert user.startswith("MASKED:[장애 요약]")


# ---------------------------------------------------------------- d) current_rules

def test_current_rules_none_keeps_numbers_for_all():
    rules = _evidence()["incident"]["fired_rules"]
    assert rules == [{"rule": "R1", "detail": "error_rate 0.40, 5xx 40/100"},
                     {"rule": "R3", "detail": "rpm 100, 기준선 300"}]


def test_current_rules_list_marks_others_as_previous_window():
    ev = _evidence(rules=("R1", "R2", "R3"), current_rules=["R3"])
    assert ev["incident"]["fired_rules"] == [{"rule": "R1", "detail": "이전 창에서 켜짐"},
                                            {"rule": "R2", "detail": "이전 창에서 켜짐"},
                                            {"rule": "R3", "detail": "rpm 100, 기준선 300"}]
    text = render_evidence(ev)
    assert "R1(이전 창에서 켜짐); R2(이전 창에서 켜짐); R3(rpm 100, 기준선 300)" in text


def test_current_rules_empty_list_and_constant():
    assert PREVIOUS_WINDOW_DETAIL == "이전 창에서 켜짐"
    ev = _evidence(rules=("R4",), current_rules=[], records=None)
    assert ev["incident"]["fired_rules"] == [{"rule": "R4", "detail": "이전 창에서 켜짐"}]


# ---------------------------------------------------------------- e) 런북 문구 중립화

def test_runbooks_do_not_mention_test_tools():
    for f in sorted(RUNBOOK_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        for word in ("loadgen", "부하 생성기", "--error-rate", "--slow-rate", "장애 주입"):
            assert word not in text, (f.name, word)


def test_runbooks_keep_path_facts():
    text = load_runbooks(RUNBOOK_DIR)
    assert "/error는 항상 RuntimeError로 500을 내는 점검용 경로다." in text
    assert "/slow?delay=N은 N초(최대 10초) 지연 뒤 응답하는 경로다." in text
