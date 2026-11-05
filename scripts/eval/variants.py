"""프롬프트·런북 조건 변형 (A1-T6a). watcher는 수정하지 않고 이 폴더에서만 비교용 조건을 만든다.

- SYSTEM_PROMPT_V2: 비교용 프롬프트. 운영(analysis.SYSTEM_PROMPT_V1)에 승격하기 전의 후보다.
- load_runbook_blocks / select_runbooks: 런북을 블록으로 나눠 켜진 규칙에 맞는 블록만 고른다.
- build_messages_variant: prompt "v1"이면 운영 analysis.build_messages 그대로, "v2"면 v2 system으로 조립한다.
- CONDITIONS: 조건 이름 → (프롬프트 버전, 런북 모드). 모드는 none(없음) / all(전체) / match(규칙에 맞는 것만).
"""
import copy
import re
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
_WATCHER = REPO_ROOT / "watcher"
if _WATCHER.is_dir() and str(_WATCHER) not in sys.path:       # watcher 코드를 설치 없이 불러온다(수정하지 않는다)
    sys.path.insert(0, str(_WATCHER))

import analysis  # noqa: E402

SYSTEM_PROMPT_V2 = """너는 웹 서비스 장애 분석 보조다. 반드시 한국어로만 답한다(영어는 서비스·도구 이름에만 쓰고, 중국어·일본어 문자는 쓰지 않는다).
[장애 요약]에 있는 사실이 가장 중요한 근거다. [런북]은 참고 자료이며 장애 요약과 맞는 항목만 쓴다. 없는 로그, 숫자, 이름을 지어내지 않는다.
규칙:
1. 원인 후보는 시스템의 어느 부분(경로, 컨테이너, DB, 워커 등)이 문제인지로 쓴다. 규칙 이름(R1~R4)이나 지표 이름을 원인으로 쓰지 않는다.
2. 장애 요약에서 이미 정상으로 확인된 것은 원인으로 쓰지 않는다. 예: 5xx가 없는 경로, 프로브가 정상(HTTP 200)이면 앱 정지나 워커 포화는 원인이 아니다.
3. 근거가 부족하면 지어내지 말고 두 번째 원인 칸에 "알 수 없음: 이유"를 쓴다.
4. 확인 항목은 원인 후보마다 하나씩, 바로 실행할 수 있게 쓴다.
출력은 JSON 하나만 쓴다: {"summary": 한 문장(150자 이내), "causes": [원인 후보 2~3개, 각각 한 문장], "checks": [확인할 항목 2~3개, 각각 한 문장]}"""  # noqa: E501  (지시문 원문 그대로. 한 글자도 바꾸지 않는다)

SYSTEM_PROMPT_V3 = """너는 웹 서비스 장애 분석 보조다. 반드시 한국어로만 답한다(영어는 서비스·도구 이름에만 쓰고, 중국어·일본어 문자는 쓰지 않는다).
[장애 요약]에 있는 사실이 가장 중요한 근거다. [런북]은 참고 자료이며 장애 요약과 맞는 항목만 쓴다. 없는 로그, 숫자, 이름을 지어내지 않는다.
규칙:
1. 원인 후보는 시스템의 어느 부분(경로, 컨테이너, DB, 워커 등)이 문제인지로 쓴다. 규칙 이름(R1~R4)이나 지표 이름을 원인으로 쓰지 않고, 요청이 줄었다, 오류가 늘었다 같은 관측을 되풀이하지 않는다.
2. 장애 요약에서 이미 정상으로 확인된 것은 원인으로 쓰지 않는다. 예: 5xx가 없는 경로, 프로브가 정상(HTTP 200)이면 앱 정지나 워커 포화는 원인이 아니다.
3. 근거가 지지하는 원인만 쓴다. 지지되는 원인이 하나뿐이면 하나만 쓴다(1~3개). 근거가 부족해 원인을 정할 수 없으면 원인 후보 하나에 "알 수 없음"으로 시작하고 콜론 뒤에 실제 이유를 한 문장으로 쓴다.
4. 확인 항목은 바로 실행할 수 있게 쓴다(1~3개). 장애 요약에 이미 나온 사실을 다시 확인하라고 쓰지 않는다.
출력은 JSON 하나만 쓴다: {"summary": 한 문장(150자 이내), "causes": [원인 후보 1~3개, 각각 한 문장], "checks": [확인할 항목 1~3개, 각각 한 문장]}"""  # noqa: E501  (지시문 원문 그대로. 한 글자도 바꾸지 않는다)

# v3 스키마: 운영 스키마의 깊은 복사에서 causes·checks의 minItems만 1로 바꾼다(maxItems는 3 그대로)
ANALYSIS_SCHEMA_V3 = copy.deepcopy(analysis.ANALYSIS_SCHEMA)
for _key in ("causes", "checks"):
    ANALYSIS_SCHEMA_V3["properties"][_key]["minItems"] = 1

# 조건 이름 → (프롬프트 버전, 런북 모드)
CONDITIONS = {"nr_v1": ("v1", "none"), "all_v1": ("v1", "all"), "all_v2": ("v2", "all"),
              "match_v1": ("v1", "match"), "match_v2": ("v2", "match"), "all_v3": ("v3", "all")}

_RULES_IN_TITLE = re.compile(r"\(규칙([^)]*)\)")      # 제목의 "(규칙 R1)", "(규칙 R4, 프로브 연속 실패)" 괄호
_RULE_ID = re.compile(r"R[1-4]")


def load_runbook_blocks(directory) -> list:
    """analysis.load_runbooks와 같은 규칙('초안:' 줄 건너뜀, 파일 이름순)으로 블록 목록을 돌려준다.
    블록마다 {"text": "[런북] 제목\\n본문", "rules": 제목의 (규칙 ...) 괄호 안 R1~R4 집합}.
    블록 text를 "\\n\\n"으로 이으면 load_runbooks의 결과와 같다."""
    d = Path(directory)
    if not d.is_dir():
        return []
    blocks = []
    for f in sorted(d.glob("*.md")):
        lines = f.read_text(encoding="utf-8").splitlines()
        while lines and (not lines[0].strip() or lines[0].strip().startswith(analysis.DRAFT_MARK)):
            lines.pop(0)
        if not lines:
            continue
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
        m = _RULES_IN_TITLE.search(title)
        blocks.append({"text": f"[런북] {title}\n{body}" if body else f"[런북] {title}",
                       "rules": set(_RULE_ID.findall(m.group(1))) if m else set()})
    return blocks


def select_runbooks(blocks, rule_ids) -> str:
    """켜진 규칙과 겹치는 블록만 파일 순서대로 "\\n\\n"으로 이은 글. 겹치는 것이 없으면 ""."""
    wanted = set(rule_ids)
    return "\n\n".join(b["text"] for b in blocks if b["rules"] & wanted)


_SYSTEM_PROMPTS = {"v2": SYSTEM_PROMPT_V2, "v3": SYSTEM_PROMPT_V3}


def _system_text_v2(runbooks_text: str, prompt: str = "v2") -> str:
    return _SYSTEM_PROMPTS[prompt] + ("\n\n" + runbooks_text if runbooks_text else "")


def schema_for(prompt: str) -> dict:
    """프롬프트 버전에 맞는 출력 스키마. v1·v2는 운영 스키마, v3는 minItems 1 스키마."""
    if prompt in ("v1", "v2"):
        return analysis.ANALYSIS_SCHEMA
    if prompt == "v3":
        return ANALYSIS_SCHEMA_V3
    raise ValueError(f"프롬프트 버전은 v1, v2, v3 중 하나여야 한다: {prompt!r}")


def parse_answer_variant(text, schema: dict):
    """analysis.parse_answer를 부르되 그동안만 analysis.ANALYSIS_SCHEMA를 schema로 바꿔 둔다.
    parse_answer가 모듈 전역을 호출 시점에 읽기 때문에 가능하다. 예외가 나도 원래대로 복원된다."""
    with mock.patch.object(analysis, "ANALYSIS_SCHEMA", schema):
        return analysis.parse_answer(text)


def build_messages_variant(evidence: dict, runbooks_text: str, prompt: str) -> list[dict]:
    """prompt "v1"은 운영 코드 그대로, "v2"·"v3"는 system만 바꾼다(근거 축소 규칙과 mask는 운영과 같다)."""
    if prompt == "v1":
        return analysis.build_messages(evidence, runbooks_text)
    if prompt not in _SYSTEM_PROMPTS:
        raise ValueError(f"프롬프트 버전은 v1, v2, v3 중 하나여야 한다: {prompt!r}")
    system = _system_text_v2(runbooks_text, prompt)
    if (analysis.estimate_tokens(system) + analysis.estimate_tokens(analysis.render_evidence(evidence))
            > analysis.PROMPT_TOKEN_BUDGET):
        evidence = analysis._shrunk(evidence)
    return [{"role": "system", "content": system},
            {"role": "user", "content": analysis.mask(analysis.render_evidence(evidence))}]
