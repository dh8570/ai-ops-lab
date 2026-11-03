"""장애 분석 답의 자동 채점 (A1-T5a). 순수 함수만, 표준 라이브러리만 쓴다.

입력은 시나리오 dict(expect 키워드 포함)와 analysis.parse_answer의 결과(ParsedAnalysis 또는 None)다.
호출 실패(None)나 형식 오류는 모든 항목이 거짓이다. 호출 통계(wall_ms 등)는 점수와 따로 기록하므로 여기에 없다.
"""
import re

_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ITEM_NUMBERS = frozenset({"1", "2", "3"})      # 목록 번호일 수 있어 지어낸 숫자 검사에서 뺀다
UNKNOWN_MARK = "알 수 없음"

SCORE_KEYS = ("format_ok", "language_ok", "cause_ok", "check_ok", "no_hallucination")


def _has_any(items, keywords) -> bool:
    """items 중 하나라도 keywords 중 하나를 (대소문자 무시하고) 포함하는가."""
    lowered = [k.lower() for k in keywords]
    return any(k in item.lower() for item in items for k in lowered)


def forbidden_hits(texts, keywords) -> list:
    """texts에 나온 금지 키워드 목록(keywords 순서, 중복 없음)."""
    blob = "\n".join(texts).lower()
    return [k for k in keywords if k.lower() in blob]


def invented_numbers(texts, *sources) -> list:
    """답에 나온 숫자 중 sources(근거·런북 글) 어디에도 문자열로 없는 것. 정수 1~3은 뺀다. 나온 순서, 중복 없음."""
    seen: list = []
    for num in _NUMBER.findall("\n".join(texts)):
        if num in _ITEM_NUMBERS or num in seen:
            continue
        if not any(num in src for src in sources):
            seen.append(num)
    return seen


def score_answer(scenario: dict, parsed, evidence_text: str, runbooks_text: str) -> dict:
    """답 하나를 채점한다. parsed가 None이면(호출 실패) 전부 거짓이다."""
    expect = scenario["expect"]
    expect_unknown = bool(expect.get("expect_unknown"))
    ok = parsed is not None and parsed.ok
    score = {"format_ok": ok, "language_ok": False, "cause_ok": False, "check_ok": False,
             "forbidden_hits": [], "no_hallucination": False, "invented_numbers": [],
             "unknown_ok": False if expect_unknown else None, "passed": False}
    if not ok:
        return score

    texts = [parsed.summary, *parsed.causes, *parsed.checks]
    score["language_ok"] = "foreign_script" not in parsed.flags
    score["cause_ok"] = _has_any(parsed.causes, expect["cause_keywords"])
    score["check_ok"] = _has_any(parsed.checks, expect["check_keywords"])
    score["forbidden_hits"] = forbidden_hits(texts, expect["forbidden_keywords"])
    score["no_hallucination"] = not score["forbidden_hits"]
    score["invented_numbers"] = invented_numbers(texts, evidence_text, runbooks_text)
    if expect_unknown:
        # "알 수 없음"이 있거나, (금지 키워드가 없고 원인 키워드가 맞음)
        score["unknown_ok"] = any(UNKNOWN_MARK in t for t in texts) or (
            score["no_hallucination"] and score["cause_ok"])
    cause_part = score["unknown_ok"] if expect_unknown else score["cause_ok"]
    score["passed"] = bool(score["format_ok"] and score["language_ok"] and cause_part
                           and score["check_ok"] and score["no_hallucination"])
    return score
