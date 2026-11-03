"""장애 분석 답의 자동 채점 (A1-T5a). 순수 함수만, 표준 라이브러리만 쓴다.

입력은 시나리오 dict(expect 키워드 포함)와 analysis.parse_answer의 결과(ParsedAnalysis 또는 None)다.
호출 실패(None)나 형식 오류는 모든 항목이 거짓이다. 호출 통계(wall_ms 등)는 점수와 따로 기록하므로 여기에 없다.

T5b 추가: 숫자 경계 비교(invented_numbers), 원인별 지지 여부(cause_matches·unsupported_causes·strict_passed),
키워드 v2(with_keywords), 규칙 되풀이 원인 수(rule_echo_causes, 보고용).
T5c 추가: strict_v3_passed. passed의 규칙(v1)은 바뀌지 않는다.
"""
import re

_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ITEM_NUMBERS = frozenset({"1", "2", "3"})      # 목록 번호일 수 있어 지어낸 숫자 검사에서 뺀다
UNKNOWN_MARK = "알 수 없음"
_RULE_NAME = re.compile(r"R[1-4]")               # 규칙 이름(R1~R4)을 원인으로 되풀이하는 동어반복을 센다

SCORE_KEYS = ("format_ok", "language_ok", "cause_ok", "check_ok", "no_hallucination")
KEYWORD_VERSIONS = ("v1", "v2")


def with_keywords(scenario: dict, version: str = "v1") -> dict:
    """키워드 버전을 고른다. v1은 시나리오를 그대로 돌려주고, v2는 scenario["expect_v2"]에 든 목록으로
    expect의 같은 키를 덮은 복사본을 돌려준다(expect_v2가 없는 시나리오는 v1과 같다). 원본은 바꾸지 않는다."""
    if version not in KEYWORD_VERSIONS:
        raise ValueError(f"키워드 버전은 {KEYWORD_VERSIONS} 중 하나여야 한다: {version!r}")
    if version == "v1":
        return scenario
    return {**scenario, "expect": {**scenario["expect"], **scenario.get("expect_v2", {})}}


def _has_any(items, keywords) -> bool:
    """items 중 하나라도 keywords 중 하나를 (대소문자 무시하고) 포함하는가."""
    lowered = [k.lower() for k in keywords]
    return any(k in item.lower() for item in items for k in lowered)


def forbidden_hits(texts, keywords) -> list:
    """texts에 나온 금지 키워드 목록(keywords 순서, 중복 없음)."""
    blob = "\n".join(texts).lower()
    return [k for k in keywords if k.lower() in blob]


def _appears(num: str, text: str) -> bool:
    """num이 text에 숫자 경계로 나오는가. 앞에 숫자나 '숫자.'가 붙거나 뒤에 숫자나 '.숫자'가 붙으면 다른 숫자의 일부다
    (예: 20은 520·20.5 안에 있어도 일치가 아니다). 문장 끝의 점('478.')은 소수점이 아니라서 경계로 본다."""
    return re.search(r"(?<![0-9])(?<![0-9]\.)" + re.escape(num) + r"(?![0-9])(?!\.[0-9])", text) is not None


def invented_numbers(texts, *sources) -> list:
    """답에 나온 숫자 중 sources(근거·런북 글) 어디에도 숫자 경계로 나오지 않는 것.
    정수 1~3은 뺀다. 나온 순서, 중복 없음."""
    seen: list = []
    for num in _NUMBER.findall("\n".join(texts)):
        if num in _ITEM_NUMBERS or num in seen:
            continue
        if not any(_appears(num, src) for src in sources):
            seen.append(num)
    return seen


def score_answer(scenario: dict, parsed, evidence_text: str, runbooks_text: str) -> dict:
    """답 하나를 채점한다. parsed가 None이면(호출 실패) 전부 거짓이다."""
    expect = scenario["expect"]
    expect_unknown = bool(expect.get("expect_unknown"))
    ok = parsed is not None and parsed.ok
    score = {"format_ok": ok, "language_ok": False, "cause_ok": False, "check_ok": False,
             "forbidden_hits": [], "no_hallucination": False, "invented_numbers": [],
             "unknown_ok": False if expect_unknown else None, "passed": False,
             "cause_matches": 0, "unsupported_causes": 0, "strict_passed": False, "rule_echo_causes": 0,
             "strict_v3_passed": False}
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
    # 원인 후보마다 키워드를 포함하는지 센다. 키워드도 없고 "알 수 없음"도 아닌 원인은 근거 없는 원인이다.
    matched = [_has_any([c], expect["cause_keywords"]) for c in parsed.causes]
    score["cause_matches"] = sum(matched)
    score["unsupported_causes"] = sum(1 for c, m in zip(parsed.causes, matched, strict=True)
                                      if not m and UNKNOWN_MARK not in c)
    cause_part = score["unknown_ok"] if expect_unknown else score["cause_ok"]
    score["passed"] = bool(score["format_ok"] and score["language_ok"] and cause_part
                           and score["check_ok"] and score["no_hallucination"])
    score["strict_passed"] = score["passed"] and score["unsupported_causes"] == 0
    score["rule_echo_causes"] = sum(1 for c in parsed.causes if _RULE_NAME.search(c))      # 보고용. passed에 안 쓴다
    # strict_v3(T5c): strict이고 규칙 이름 되풀이 원인이 없고, expect_unknown이면 답에 "알 수 없음"이 있다
    score["strict_v3_passed"] = bool(score["strict_passed"] and score["rule_echo_causes"] == 0
                                     and (not expect_unknown or any(UNKNOWN_MARK in t for t in texts)))
    return score
