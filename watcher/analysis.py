"""장애 하나를 'LLM에 보낼 안전한 근거'로 바꾸고 프롬프트를 조립하는 순수 함수들.

- 원문을 보내지 않는다(M0): 근거에는 숫자, 건수, 정해진 이름만 들어가고 로그의 자유 문장은 들어가지 않는다.
- 입출력만 있는 함수라 같은 입력이면 같은 출력이다. (load_runbooks만 지정한 폴더의 파일을 읽는다)
- 마스킹은 아직 없다. mask()는 나중에 들어올 자리이며 프롬프트 조립은 반드시 이 함수를 거친다.
"""
import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROMPT_VERSION = "v1"

# 토큰 수는 보수적으로 "글자 수 × 1.0"으로 추정한다 (실측: 한국어 혼합 63자 → 43토큰).
NUM_CTX = 4096
MAX_OUTPUT_TOKENS = 512
PROMPT_TOKEN_BUDGET = 3000

MAX_PATH_LEN = 80
MAX_KIND_LEN = 60
MAX_TOP_PATHS, MAX_TOP_KINDS = 5, 5
SHRUNK_TOP_PATHS, SHRUNK_TOP_KINDS = 3, 2
MAX_FIRED_RULES = 4

OTHER_PATH = "/기타"
OTHER_KIND = "기타"

SUMMARY_MAX_CHARS = 300
ITEM_MAX_CHARS = 200

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "causes": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
        "checks": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
    },
    "required": ["summary", "causes", "checks"],
}

SYSTEM_PROMPT_V1 = """너는 웹 서비스 장애 분석 보조다. 반드시 한국어로만 답한다(영어는 서비스·도구 이름에만 쓰고, 중국어·일본어 문자는 쓰지 않는다).
아래 [런북]과 사용자가 준 [장애 요약]에 있는 사실만 근거로 쓴다. 없는 로그, 숫자, 원인을 지어내지 않는다.
근거가 부족하면 해당 칸에 "알 수 없음"이라고 쓴다.
출력은 JSON 하나만 쓴다: {"summary": 한 문장(150자 이내), "causes": [원인 후보 2~3개, 각각 한 문장], "checks": [확인할 항목 2~3개, 각각 한 문장]}"""  # noqa: E501  (지시문 원문 그대로. 한 글자도 바꾸지 않는다)

DRAFT_MARK = "초안:"


# ---------------------------------------------------------------- 원문을 걸러 내는 함수들

def mask(text: str) -> str:
    """마스킹 자리. 지금은 항등이다. 나중에 IP·토큰·이메일 마스킹이 여기로 들어온다."""
    return text


_PATH_OK = re.compile(r"/[A-Za-z0-9_./{}-]*")
_DIGITS = re.compile(r"[0-9]+")
_HEX8 = re.compile(r"[0-9A-Fa-f]{8,}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_IPV4 = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}")
_KIND_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,59}")
_RULE_OK = re.compile(r"R[0-9]{1,2}")
_HTTP_CODE = re.compile(r"HTTP [0-9]{3}")


def _is_id_segment(seg: str) -> bool:
    return bool(_DIGITS.fullmatch(seg) or _HEX8.fullmatch(seg) or _UUID.fullmatch(seg)
                or _IPV4.fullmatch(seg) or len(seg) >= 32)


def normalize_path(path) -> str:
    """경로를 집계용 모양으로 정규화한다. 쿼리·프래그먼트 제거, id처럼 보이는 구간은 {id}, 이상한 문자는 /기타."""
    if not isinstance(path, str):
        return OTHER_PATH
    path = path.split("#", 1)[0].split("?", 1)[0]
    out = "/".join("{id}" if _is_id_segment(seg) else seg for seg in path.split("/"))[:MAX_PATH_LEN]
    return out if _PATH_OK.fullmatch(out) else OTHER_PATH


def exception_kind(error_text) -> str:
    """오류 필드('종류: 메시지')에서 종류만 꺼낸다. 메시지는 어디에도 남기지 않는다."""
    if not isinstance(error_text, str) or ":" not in error_text:
        return OTHER_KIND
    head = error_text.split(":", 1)[0]
    return head if _KIND_OK.fullmatch(head) else OTHER_KIND


def _reason_code(detail) -> str:
    """프로브 결과 문자열(예: 'HTTP 503', 'timeout', 'ConnectionRefusedError')을 정해진 코드로 줄인다."""
    if not isinstance(detail, str):
        return OTHER_KIND
    if _HTTP_CODE.fullmatch(detail) or _KIND_OK.fullmatch(detail):
        return detail
    return OTHER_KIND


# ---------------------------------------------------------------- 근거(evidence)

def estimate_tokens(text: str) -> int:
    return len(text)


def _system_text(runbooks_text: str) -> str:
    return SYSTEM_PROMPT_V1 + "\n\n[런북]\n" + runbooks_text


def _status_bucket(status) -> str | None:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    return {2: "2xx", 4: "4xx", 5: "5xx"}.get(code // 100)


def _aggregate(records, exclude_paths) -> tuple[Counter, dict]:
    kinds: Counter = Counter()
    paths: dict[str, dict] = {}
    excluded = {normalize_path(p) for p in exclude_paths}
    for r in records:
        if not isinstance(r, dict):
            continue
        path = normalize_path(r.get("path"))
        if path in excluded:
            continue
        entry = paths.setdefault(path, {"count": 0, "2xx": 0, "4xx": 0, "5xx": 0})
        entry["count"] += 1
        bucket = _status_bucket(r.get("status"))
        if bucket:
            entry[bucket] += 1
        err = r.get("error")
        if isinstance(err, str) and err:
            kinds[exception_kind(err)] += 1
    return kinds, paths


def _fired_rule_detail(rule: str, ws, baseline_rpm, probe_view) -> str:
    """규칙별 설명은 Finding.detail을 믿지 않고 숫자에서 다시 만든다."""
    if rule == "R1":
        return f"error_rate {ws.error_rate:.2f}, 5xx {ws.errors_5xx}/{ws.requests}"
    if rule == "R2":
        return f"p95 {ws.p95_ms:.0f}ms"
    if rule == "R3":
        return f"rpm {ws.rpm:.0f}" + (f", 기준선 {baseline_rpm:.0f}" if baseline_rpm is not None else "")
    if rule == "R4":
        return f"연속 실패 {probe_view['consecutive_failures']}회" if probe_view else "프로브 연속 실패"
    return ""


def _number(v):
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


def _baseline_view(baseline):
    """baseline은 숫자(rpm 중앙값) 또는 {rpm, error_rate, p95_ms} 중 가능한 것만 든 dict."""
    if baseline is None:
        return None
    if isinstance(baseline, dict):
        view = {k: round(float(baseline[k]), 3) for k in ("rpm", "error_rate", "p95_ms")
                if _number(baseline.get(k)) is not None}
        return view or None
    return {"rpm": round(float(baseline), 1)} if _number(baseline) is not None else None


def _probe_view(probe):
    """probe는 Prober.probe()의 dict(consecutive_failures, detail) 또는 Prober 객체."""
    if probe is None:
        return None
    if isinstance(probe, dict):
        n, detail = probe.get("consecutive_failures"), probe.get("detail", probe.get("last_detail"))
    else:
        n, detail = getattr(probe, "consecutive_failures", None), getattr(probe, "last_detail", None)
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    return {"consecutive_failures": n, "last_reason_code": _reason_code(detail)}


def build_evidence(incident, window_stats, records, baseline, probe, now_ts, *,
                   window_sec: int = 60, exclude_paths: tuple = ("/healthz",), runbooks_text: str = "") -> dict:
    """장애 하나를 LLM에 보낼 근거 dict로 만든다. 숫자, 건수, 정해진 이름만 담는다.

    records=None이면(창 사이에 R4가 켜진 경우) top_* 는 비우고 records_available=False.
    runbooks_text는 토큰 추정에만 쓴다. 추정이 PROMPT_TOKEN_BUDGET을 넘으면 top_paths·top_error_kinds를 줄인다.
    """
    ws = window_stats
    kinds, paths = _aggregate(records, exclude_paths) if records is not None else (Counter(), {})
    probe_view = _probe_view(probe)
    baseline_view = _baseline_view(baseline)
    baseline_rpm = baseline_view.get("rpm") if baseline_view else None

    opened = datetime.fromisoformat(incident.opened_at).timestamp()
    rules = sorted({r for r in incident.rules if isinstance(r, str) and _RULE_OK.fullmatch(r)})[:MAX_FIRED_RULES]
    severity = incident.severity if incident.severity in ("warning", "critical") else OTHER_KIND

    def assemble(max_paths: int, max_kinds: int) -> dict:
        top_kinds = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:max_kinds]
        top_paths = sorted(paths.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:max_paths]
        return {
            "incident": {
                "id": int(incident.id),
                "severity": severity,
                "opened_min_ago": max(0, int((now_ts - opened) // 60)),
                "fired_rules": [{"rule": r, "detail": _fired_rule_detail(r, ws, baseline_rpm, probe_view)}
                                for r in rules],
            },
            "window": {
                "sec": int(window_sec),
                "requests": int(ws.requests),
                "errors_5xx": int(ws.errors_5xx),
                "error_rate": round(ws.error_rate, 3),
                "p95_ms": round(ws.p95_ms, 1),
                "rpm": round(ws.rpm, 1),
            },
            "baseline": baseline_view,
            "top_error_kinds": [{"kind": k[:MAX_KIND_LEN], "count": c} for k, c in top_kinds],
            "top_paths": [{"path": p, "count": e["count"],
                           "status_counts": {"2xx": e["2xx"], "4xx": e["4xx"], "5xx": e["5xx"]}}
                          for p, e in top_paths],
            "probe": probe_view,
            "records_available": records is not None,
        }

    evidence = assemble(MAX_TOP_PATHS, MAX_TOP_KINDS)
    if _prompt_tokens(evidence, runbooks_text) > PROMPT_TOKEN_BUDGET:
        evidence = assemble(SHRUNK_TOP_PATHS, SHRUNK_TOP_KINDS)
    return evidence


def _shrunk(evidence: dict) -> dict:
    ev = copy.deepcopy(evidence)
    ev["top_paths"] = ev["top_paths"][:SHRUNK_TOP_PATHS]
    ev["top_error_kinds"] = ev["top_error_kinds"][:SHRUNK_TOP_KINDS]
    return ev


def _prompt_tokens(evidence: dict, runbooks_text: str) -> int:
    return estimate_tokens(_system_text(runbooks_text)) + estimate_tokens(render_evidence(evidence))


# ---------------------------------------------------------------- 프롬프트 조립

def render_evidence(ev: dict) -> str:
    """근거를 줄 단위 한국어 요약으로 바꾼다 (키 순서 고정, 없는 필드의 줄은 생략)."""
    inc, w = ev["incident"], ev["window"]
    rules = "; ".join(f"{r['rule']}({r['detail']})" if r["detail"] else r["rule"] for r in inc["fired_rules"])
    started = f"{inc['opened_min_ago']}분 전" if inc["opened_min_ago"] else "방금"
    lines = [
        "[장애 요약]",
        f"장애 #{inc['id']} · 심각도 {inc['severity']} · 시작 {started} · 켜진 규칙: {rules or '없음'}",
        f"[최근 {w['sec']}초] 요청 {w['requests']} · 5xx {w['errors_5xx']} · 에러율 {w['error_rate']:.2f}"
        f" · p95 {w['p95_ms']:.0f}ms · 분당 요청 {w['rpm']:.0f}",
    ]
    b = ev["baseline"]
    if b:
        parts = []
        if "error_rate" in b:
            parts.append(f"평소 에러율 {b['error_rate']:.2f}")
        if "p95_ms" in b:
            parts.append(f"평소 p95 {b['p95_ms']:.0f}ms")
        if "rpm" in b:
            parts.append(f"평소 분당 요청 {b['rpm']:.0f}")
        lines.append("[기준선] " + " · ".join(parts))
    if not ev["records_available"]:
        lines.append("[참고] 이 장애는 로그 창이 닫히기 전에 열려 오류 유형·경로 집계가 없음")
    if ev["top_error_kinds"]:
        lines.append("[오류 유형] " + " · ".join(f"{k['kind']} {k['count']}건" for k in ev["top_error_kinds"]))
    if ev["top_paths"]:
        def one(p):
            sc = " ".join(f"{name} {n}" for name, n in p["status_counts"].items() if n)
            return f"{p['path']} {p['count']}건" + (f"({sc})" if sc else "")
        lines.append("[경로] " + " · ".join(one(p) for p in ev["top_paths"]))
    if ev["probe"]:
        pr = ev["probe"]
        lines.append(f"[프로브] 연속 실패 {pr['consecutive_failures']}회 · 최근 결과 {pr['last_reason_code']}")
    return "\n".join(lines)


def load_runbooks(directory) -> str:
    """폴더의 *.md를 파일 이름순으로 읽어 '[런북] 제목\\n본문' 블록들을 잇는다. 맨 위 '초안:' 줄은 건너뛴다."""
    d = Path(directory)
    if not d.is_dir():
        return ""
    blocks = []
    for f in sorted(d.glob("*.md")):
        lines = f.read_text(encoding="utf-8").splitlines()
        while lines and (not lines[0].strip() or lines[0].strip().startswith(DRAFT_MARK)):
            lines.pop(0)
        if not lines:
            continue
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
        blocks.append(f"[런북] {title}\n{body}" if body else f"[런북] {title}")
    return "\n\n".join(blocks)


def build_messages(evidence: dict, runbooks_text: str) -> list[dict]:
    """system(바뀌지 않는 지시 + 런북)과 user(이번 장애)를 나눠 담는다. 모든 글은 mask()를 거친다."""
    if _prompt_tokens(evidence, runbooks_text) > PROMPT_TOKEN_BUDGET:
        evidence = _shrunk(evidence)
    return [
        {"role": "system", "content": mask(_system_text(runbooks_text))},
        {"role": "user", "content": mask(render_evidence(evidence))},
    ]


# ---------------------------------------------------------------- 답 파싱과 검사

@dataclass
class ParsedAnalysis:
    ok: bool
    reason: str = ""
    summary: str = ""
    causes: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    flags: list = field(default_factory=list)


# 한글·ASCII·숫자·일반 문장부호 밖의 문자 중 한자, 가나, 태국, 키릴, 아랍 문자
_FOREIGN_RANGES = (
    (0x0400, 0x052F),                                                   # 키릴
    (0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF),   # 아랍
    (0x0E00, 0x0E7F),                                                   # 태국
    (0x3040, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9F),               # 히라가나·가타카나(반각 포함)
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F),   # 한자(CJK)
)


def _has_foreign_script(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _FOREIGN_RANGES)


def _schema_error(value, schema: dict, where: str) -> str | None:
    """ANALYSIS_SCHEMA가 쓰는 부분(type, properties, required, items, minItems, maxItems)만 직접 검사한다."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return f"{where}: object 아님"
        for key in schema.get("required", []):
            if key not in value:
                return f"{where}.{key}: 없음"
        for key, sub in schema.get("properties", {}).items():
            if key in value and (err := _schema_error(value[key], sub, f"{where}.{key}")):
                return err
    elif t == "array":
        if not isinstance(value, list):
            return f"{where}: array 아님"
        if len(value) < schema.get("minItems", 0):
            return f"{where}: 항목 {len(value)}개 (최소 {schema['minItems']})"
        if len(value) > schema.get("maxItems", len(value)):
            return f"{where}: 항목 {len(value)}개 (최대 {schema['maxItems']})"
        for i, item in enumerate(value):
            if err := _schema_error(item, schema.get("items", {}), f"{where}[{i}]"):
                return err
    elif t == "string" and not isinstance(value, str):
        return f"{where}: string 아님"
    return None


def parse_answer(text) -> ParsedAnalysis:
    """모델 답을 JSON으로 읽고 스키마를 검사한다. 항목 수·길이 상한을 넘으면 잘라 내고 trimmed 표시."""
    bad = ParsedAnalysis(ok=False, reason="bad_output")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return bad
    if not isinstance(data, dict):
        return bad

    flags: list[str] = []
    props = ANALYSIS_SCHEMA["properties"]
    for key in ("causes", "checks"):
        items = data.get(key)
        if isinstance(items, list) and len(items) > props[key]["maxItems"]:
            data[key] = items[:props[key]["maxItems"]]
            flags.append("trimmed")
    if _schema_error(data, ANALYSIS_SCHEMA, "$"):
        return bad

    summary = data["summary"]
    causes, checks = list(data["causes"]), list(data["checks"])
    if len(summary) > SUMMARY_MAX_CHARS or any(len(s) > ITEM_MAX_CHARS for s in causes + checks):
        flags.append("trimmed")
    summary = summary[:SUMMARY_MAX_CHARS]
    causes = [s[:ITEM_MAX_CHARS] for s in causes]
    checks = [s[:ITEM_MAX_CHARS] for s in checks]

    every = [summary, *causes, *checks]
    if _has_foreign_script("\n".join(every)):
        flags.append("foreign_script")
    if any("알 수 없음" in s for s in every):
        flags.append("unknown_marker")
    if any(not s.strip() for s in every):
        flags.append("empty_field")
    flags = list(dict.fromkeys(flags))      # trimmed가 두 번 들어가도 한 번만
    return ParsedAnalysis(ok=True, summary=summary, causes=causes, checks=checks, flags=flags)


# ---------------------------------------------------------------- Slack 본문

def _escape(text: str) -> str:
    """Slack은 & < > 세 문자만 이스케이프하면 된다 (& 를 가장 먼저)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack_text(parsed: ParsedAnalysis, model: str, wall_s: float, prompt_tokens: int, gen_tokens: int) -> str:
    def numbered(items):
        return "\n".join(f"{i}. {_escape(s) or '(비어 있음)'}" for i, s in enumerate(items, 1))

    return "\n".join([
        _escape(parsed.summary),
        "",
        "원인 후보",
        numbered(parsed.causes),
        "",
        "확인할 항목",
        numbered(parsed.checks),
        "",
        f"모델 {_escape(model)} · {wall_s:.1f}초 · 입력 {prompt_tokens}tok / 출력 {gen_tokens}tok · AI 생성, 검증 전",
    ])
