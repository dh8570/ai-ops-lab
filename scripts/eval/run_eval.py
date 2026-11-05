#!/usr/bin/env python3
"""장애 분석 채점표 실행기 (A1-T5a).

운영과 같은 프롬프트 코드(analysis.build_evidence·build_messages·parse_answer)와
호출 코드(llm_client.chat, 옵션 7개 명시)로
장애 시나리오 5개를 Ollama에 반복해서 묻고 scoring.py로 채점한다.
watcher는 수정하지 않고 Slack 등 알림은 보내지 않는다.
비교 조건은 두 가지다: no_runbook(runbooks_text="") 대 runbook(운영과 같은 load_runbooks 결과).

    python3 scripts/eval/run_eval.py --dry          # Ollama를 부르지 않고 프롬프트와 추정 토큰만 출력
    python3 scripts/eval/run_eval.py                # 5 시나리오 x 2 조건 x 8회 = 80번 호출
    python3 scripts/eval/run_eval.py --only s1,s4   # 일부 시나리오만
    python3 scripts/eval/run_eval.py --keywords v2  # T5b: 시나리오 7개 x 조건 2 x 12회 = 168번 호출(warm 포함)
    python3 scripts/eval/run_eval.py --rescore docs/eval/raw/eval-<UTC>.jsonl --keywords v1   # Ollama 없이 다시 채점
    # T6a: 프롬프트 v2와 런북 선택 비교(보류 시나리오 s8~s11 포함). 495번 호출
    python3 scripts/eval/run_eval.py --with-holdout --keywords v2 --settings warm,var \\
        --conditions nr_v1,all_v1,all_v2,match_v1,match_v2

조건 이름: no_runbook = nr_v1, runbook = all_v1(기존 이름의 별칭, 기록에는 준 이름 그대로 남긴다).
정의는 variants.CONDITIONS.

main()을 코드에서 부를 때의 기본값은 T5a와 같다(설정 8개, 시나리오 5개). 스크립트로 실행하면(__main__) T5b 값을 쓴다
(settings=SETTINGS_V2, 시나리오 폴더 scenarios + scenarios_extra).

시나리오 숫자는 합성이며 실측이 아니다. 출력: <out>/raw/eval-<UTC>.jsonl(호출마다 한 줄), <out>/scoreboard-<UTC>.md.
종료 코드: 0 정상 / 1 --rescore --keywords v1이 원본 기록의 통과 판정과 다름 / 2 연속 실패로 중단 또는 인자 오류
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
_WATCHER = REPO_ROOT / "watcher"
if _WATCHER.is_dir() and str(_WATCHER) not in sys.path:       # watcher 코드를 설치 없이 불러온다(수정하지 않는다)
    sys.path.insert(0, str(_WATCHER))

import scoring  # noqa: E402
import variants  # noqa: E402

import analysis  # noqa: E402
import config  # noqa: E402
import dispatcher  # noqa: E402
import llm_client  # noqa: E402
import state  # noqa: E402
import stats  # noqa: E402

DEFAULT_URL = "http://10.97.0.1:11434"
NOW_TS = 1_800_000_000.0                     # 근거의 '몇 분 전'을 계산하는 고정 기준 시각(같은 입력이면 같은 프롬프트)
WINDOW_SEC = 60
EXCLUDE_PATHS = ("/healthz",)
SCENARIO_DIR = HERE / "scenarios"                 # T5a 시나리오 5개(기준선. 바꾸지 않는다)
EXTRA_SCENARIO_DIR = HERE / "scenarios_extra"     # T5b 추가 시나리오(s6, s7)
HOLDOUT_DIR = HERE / "scenarios_holdout"          # T6a 보류 시나리오(s8~s11). --with-holdout일 때만 합친다
CONDITIONS = ("no_runbook", "runbook")            # 기본 조건(T5)
CONDITION_ALIASES = {"no_runbook": "nr_v1", "runbook": "all_v1"}
# (설정 이름, 반복 번호, 옵션 덮어쓰기). prod는 운영 기본값 3번(재현성), var는 temperature 0.2에 seed 1~5.
SETTINGS = ([("prod", i, {}) for i in range(1, 4)]
            + [("var", i, {"temperature": 0.2, "seed": i}) for i in range(1, 6)])
# T5b: prod 3 + warmup 1 + warm 3 + var 5 = 12. warmup은 같은 요청을 먼저 한 번 보내 캐시를 데우는 호출이며
# 기록은 하되 통과율·재현성 집계에서 뺀다. warm의 옵션은 prod와 같다(덮어쓰기 없음).
SETTINGS_V2 = ([("prod", i, {}) for i in range(1, 4)] + [("warmup", 1, {})] + [("warm", i, {}) for i in range(1, 4)]
               + [("var", i, {"temperature": 0.2, "seed": i}) for i in range(1, 6)])
SETTING_NAMES = ("prod", "warmup", "warm", "var")
REPORT_SETTINGS = (("prod", 3), ("warm", 3), ("var", 5))      # 표에 보이는 설정과 설정별 반복 수(warmup은 없음)
MAX_CONSECUTIVE_FAILS = 5
SYNTHETIC_NOTE = "시나리오 숫자는 합성이며 실측이 아니다."
ITEMS = (("형식", "format_ok"), ("언어", "language_ok"), ("원인", "cause_ok"), ("확인", "check_ok"),
         ("환각 없음", "no_hallucination"), ("통과", "passed"), ("엄격 통과", "strict_passed"),
         ("엄격 v3 통과", "strict_v3_passed"))


# ---------------------------------------------------------------- 시나리오와 프롬프트

def _natural(text: str) -> list:
    """s10이 s1_ 앞에 오지 않도록 숫자 덩어리를 숫자로 비교하는 정렬 키."""
    return [int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", text)]


def condition_spec(name: str) -> tuple:
    """조건 이름(별칭 포함)의 (프롬프트 버전, 런북 모드)."""
    spec = variants.CONDITIONS.get(CONDITION_ALIASES.get(name, name))
    if spec is None:
        raise ValueError(f"알 수 없는 조건: {name} (가능: {', '.join([*variants.CONDITIONS, *CONDITION_ALIASES])})")
    return spec


def parse_conditions(text) -> tuple:
    """--conditions 값(쉼표 목록)을 이름 튜플로. None이면 기본 조건(no_runbook, runbook)."""
    if not text:
        return CONDITIONS
    names = tuple(n.strip() for n in text.split(",") if n.strip())
    for n in names:
        condition_spec(n)
    return names


def parse_settings(text, available) -> list:
    """--settings 값(쉼표 목록, 예: warm,var)으로 available(설정 튜플 목록)을 거른다. 순서는 available을 따른다.
    warm을 고르면 캐시를 데우는 warmup 호출도 함께 넣는다(warm 3번의 조건이 prod와 같도록)."""
    names = {n.strip() for n in text.split(",") if n.strip()}
    bad = names - set(SETTING_NAMES)
    if bad:
        raise ValueError(f"알 수 없는 설정: {', '.join(sorted(bad))} (가능: {', '.join(SETTING_NAMES)})")
    if "warm" in names:
        names.add("warmup")
    chosen = [s for s in available if s[0] in names]
    if not chosen:
        raise ValueError(f"고른 설정이 이 실행의 설정 목록에 없다: {text}")
    return chosen


def runbook_text_for(mode: str, scenario: dict, all_text: str, blocks) -> str:
    """런북 모드별 글: none은 "", all은 전체, match는 그 시나리오의 current_rules와 맞는 블록만."""
    if mode == "none":
        return ""
    if mode == "all":
        return all_text
    if mode == "match":
        return variants.select_runbooks(blocks or [], scenario["current_rules"])
    raise ValueError(f"알 수 없는 런북 모드: {mode}")


def load_scenarios(directory, only=None, keywords: str = "v1") -> list:
    """폴더(또는 폴더 목록)의 *.json을 읽어 id 순으로 돌려준다. only(['s1','s2'])는 id 전체 또는 '_' 앞 접두어와
    맞는 것만 남긴다. keywords가 v2면 expect_v2를 합친 시나리오를 돌려준다(scoring.with_keywords)."""
    dirs = [directory] if isinstance(directory, str | Path) else list(directory)
    files = [f for d in dirs for f in Path(d).glob("*.json")]
    scenarios = sorted((json.loads(f.read_text(encoding="utf-8")) for f in files), key=lambda sc: _natural(sc["id"]))
    if only:
        known = {s["id"] for s in scenarios}
        for o in only:
            if not any(i == o or i.startswith(o + "_") for i in known):
                raise ValueError(f"알 수 없는 시나리오: {o}")
        scenarios = [s for s in scenarios if any(s["id"] == o or s["id"].startswith(o + "_") for o in only)]
    return [scoring.with_keywords(sc, keywords) for sc in scenarios]


def expand_records(scenario: dict, now_ts: float = NOW_TS) -> list:
    """records 명세를 count만큼 펼쳐 app의 JSON 로그 한 줄 모양(ts, service, request_id, method, path, status,
    latency_ms, error)으로 만든다. 요청 수·비율은 여기서 세지 않고 stats.summarize가 계산한다."""
    start = datetime.fromtimestamp(now_ts - WINDOW_SEC, UTC)
    out: list = []
    for spec in scenario["records"]:
        for _ in range(spec["count"]):
            n = len(out)
            out.append({"ts": (start + timedelta(milliseconds=n)).isoformat(timespec="milliseconds"),
                        "service": "visit-counter", "request_id": f"{n:012x}", "method": "GET",
                        "path": spec["path"], "status": spec["status"], "latency_ms": spec["latency_ms"],
                        "error": spec.get("error")})
    return out


def make_incident(scenario: dict, now_ts: float = NOW_TS) -> state.Incident:
    inc = scenario["incident"]
    opened = datetime.fromtimestamp(now_ts - inc["opened_min_ago"] * 60, UTC).isoformat(timespec="seconds")
    return state.Incident(inc["id"], "visit-counter", "FIRING", inc["severity"], list(inc["rules"]),
                          opened, None, 1, 0)


def build_prompt(scenario: dict, runbooks_text: str, now_ts: float = NOW_TS, prompt: str = "v1"):
    """(근거 dict, messages, 창 통계). 운영의 dispatcher가 쓰는 함수 그대로다(prompt v2만 variants를 거친다)."""
    records = expand_records(scenario, now_ts) if scenario["records_available"] else None
    window = stats.summarize(records or [], WINDOW_SEC, EXCLUDE_PATHS)
    evidence = analysis.build_evidence(
        make_incident(scenario, now_ts), window, records, scenario["baseline_rpm"], scenario["probe"], now_ts,
        window_sec=WINDOW_SEC, exclude_paths=EXCLUDE_PATHS, runbooks_text=runbooks_text,
        current_rules=scenario["current_rules"])
    return evidence, variants.build_messages_variant(evidence, runbooks_text, prompt), window


def estimate_prompt_tokens(messages: list) -> int:
    return sum(analysis.estimate_tokens(m["content"]) for m in messages)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 실행

def _call_stats(result) -> dict:
    return {"wall_ms": result.wall_ms, "prompt_eval_count": result.prompt_eval_count,
            "prompt_eval_cached_count": result.prompt_eval_cached_count, "eval_count": result.eval_count,
            "load_ms": result.load_ms, "prompt_eval_ms": result.prompt_eval_ms, "eval_ms": result.eval_ms}


def _parsed_dict(parsed):
    return None if parsed is None else {"ok": parsed.ok, "reason": parsed.reason, "summary": parsed.summary,
                                        "causes": parsed.causes, "checks": parsed.checks, "flags": parsed.flags}


def run_all(scenarios, runbooks_text, *, chat, url, model, cfg, keep_alive, connect_timeout, total_timeout,
            now_ts: float = NOW_TS, on_record=None, settings=SETTINGS, conditions=CONDITIONS, blocks=None):
    """시나리오 → 조건 → 설정 순으로 호출한다(같은 조건의 호출이 연달아 가고, 호출은 서로 독립 요청이다).
    conditions는 조건 이름 목록(별칭 가능), blocks는 match 모드가 쓰는 variants.load_runbook_blocks 결과다.
    실패해도 이유를 기록하고 계속하며, 연속 MAX_CONSECUTIVE_FAILS번 실패하면 멈춘다.
    (기록 목록, 중단 여부)를 돌려준다."""
    records: list = []
    streak = 0
    for sc in scenarios:
        for cond in conditions:
            prompt, mode = condition_spec(cond)
            rb = runbook_text_for(mode, sc, runbooks_text, blocks)
            _, messages, _ = build_prompt(sc, rb, now_ts, prompt)
            system, evidence_text = messages[0]["content"], messages[1]["content"]
            for setting, rep, override in settings:
                options = dispatcher.options_from(cfg, **override)
                result = chat(url, model, messages, options, fmt=analysis.ANALYSIS_SCHEMA, keep_alive=keep_alive,
                              connect_timeout=connect_timeout, total_timeout=total_timeout)
                parsed = analysis.parse_answer(result.text) if result.ok else None
                rec = {"scenario": sc["id"], "condition": cond, "setting": setting, "rep": rep,
                       "model": model, "options": options.as_dict(), "system_sha256": _sha256(system),
                       "est_prompt_tokens": estimate_prompt_tokens(messages),
                       "prompt_version": prompt, "runbook_mode": mode, "runbook_chars": len(rb),
                       "holdout": bool(sc.get("holdout")),
                       "ok": result.ok, "fail_reason": result.reason, "done_reason": result.done_reason,
                       "http_status": result.http_status, "text": result.text,
                       "parsed": _parsed_dict(parsed),
                       "score": scoring.score_answer(sc, parsed, evidence_text, rb),
                       "stats": _call_stats(result)}
                records.append(rec)
                if on_record:
                    on_record(rec)
                streak = 0 if result.ok else streak + 1
                if streak >= MAX_CONSECUTIVE_FAILS:
                    return records, True
    return records, False


# ---------------------------------------------------------------- 표 렌더링

def _sel(records, **kw) -> list:
    return [r for r in records if all(r[k] == v for k, v in kw.items())]


def _frac(rows, key="passed") -> str:
    return f"{sum(1 for r in rows if r['score'][key])}/{len(rows)}" if rows else "-"


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v, digits=0) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _table(header, rows) -> list:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|",
            *("| " + " | ".join(str(c) for c in row) + " |" for row in rows)]


def _echo(rows) -> str:
    """원인 후보가 규칙 이름(R1~R4)을 포함한 호출 수/그런 원인의 수. 판정에 쓰지 않는 보고용."""
    n = [r["score"].get("rule_echo_causes", 0) for r in rows]
    return f"{sum(1 for x in n if x)}/{sum(n)}" if rows else "-"


def reproducible(rows) -> bool:
    """prod 호출 3번이 모두 성공이고 응답 글이 같은가."""
    return len(rows) == 3 and all(r["ok"] for r in rows) and len({r["text"] for r in rows}) == 1


def _conditions(records) -> tuple:
    """기록에 나온 조건 이름(처음 나온 순서). 기록이 없으면 기본 조건."""
    return tuple(dict.fromkeys(r["condition"] for r in records)) or CONDITIONS


def render_scoreboard(meta: dict, records: list, scenarios: list) -> str:
    ids = [s["id"] for s in scenarios]
    conds = _conditions(records)
    compare = bool(meta.get("compare"))
    n_warmup = sum(1 for r in records if r["setting"] == "warmup")
    L: list = [meta.get("title", "# 장애 분석 채점표 (A1-T5a)"), ""]
    if meta.get("aborted"):
        L += [f"> 주의: 연속 {MAX_CONSECUTIVE_FAILS}번 실패로 중단된 실행이다. 아래는 중단 전까지의 결과다.", ""]
    L += ["## 조건", ""]
    info = [("실행 시각(UTC)", meta["started_utc"]), ("모델", meta["model"]), ("Ollama 버전", meta["ollama_version"]),
            ("PROMPT_VERSION", meta["prompt_version"]), ("SYSTEM_PROMPT_V1 sha256", meta["system_sha256"]),
            ("옵션 7개 (prod x3, warm도 같음)", json.dumps(meta["options_prod"])),
            ("옵션 7개 (var x5, seed 1~5)", json.dumps(meta["options_var"]))]
    if "keep_alive" in meta:
        info.append(("keep_alive / 연결·총 제한(초)",
                     f"{meta['keep_alive']} / {meta['connect_timeout']:g}·{meta['total_timeout']:g}"))
    if compare:
        info.append(("SYSTEM_PROMPT_V2 sha256", meta["system_sha256_v2"]))
        info.append(("조건", ", ".join(f"{c}={'/'.join(condition_spec(c))}" for c in conds)))
    info += [("런북", f"{meta['runbook_chars']}자 (전체 런북. 런북 all 조건에서 사용, match는 규칙에 맞는 블록만)"
              if compare else f"{meta['runbook_chars']}자 (조건 runbook에서만 사용)"),
             ("호출 수", f"{len(records)} (시나리오 {len(ids)} x 조건 {len(conds)}, warmup 호출 {n_warmup}번 포함)")]
    if "keywords" in meta:
        info.append(("채점 키워드 버전", meta["keywords"]))
    if "source_raw" in meta:
        info.append(("재채점 원본", meta["source_raw"]))
    info.append(("주의", SYNTHETIC_NOTE))
    L += _table(["항목", "값"], info)

    # warmup 호출은 기록만 하고 표에서는 뺀다(아래 _sel에 setting을 항상 지정하고 warmup은 지정하지 않는다).
    present = {r["setting"] for r in records}
    cols = [(c, s, n) for c in conds for s, n in REPORT_SETTINGS
            if s in present or (not present and s in ("prod", "var"))]
    L += ["", "## ① 시나리오 x 조건 통과율", ""]
    L += _table(["시나리오", *(f"{c} {s}({n}회)" for c, s, n in cols)],
                [(i, *(_frac(_sel(records, scenario=i, condition=c, setting=s)) for c, s, _ in cols)) for i in ids]
                + [("합계", *(_frac(_sel(records, condition=c, setting=s)) for c, s, _ in cols))])

    L += ["", "## ①-2 엄격 통과율 (strict_passed: 통과이고 근거 없는 원인 0개)", ""]
    L += _table(["시나리오", *(f"{c} {s}({n}회)" for c, s, n in cols)],
                [(i, *(_frac(_sel(records, scenario=i, condition=c, setting=s), "strict_passed")
                       for c, s, _ in cols)) for i in ids]
                + [("합계", *(_frac(_sel(records, condition=c, setting=s), "strict_passed") for c, s, _ in cols))])

    L += ["", "## ①-3 엄격 v3 통과율 (엄격 통과 + 규칙 이름 되풀이 0개 + s5는 \"알 수 없음\" 필수)", ""]
    L += _table(["시나리오", *(f"{c} {s}({n}회)" for c, s, n in cols)],
                [(i, *(_frac(_sel(records, scenario=i, condition=c, setting=s), "strict_v3_passed")
                       for c, s, _ in cols)) for i in ids]
                + [("합계", *(_frac(_sel(records, condition=c, setting=s), "strict_v3_passed")
                              for c, s, _ in cols))])

    L += ["", "## ② 항목별 통과 수 (전체 시나리오 합)", "",
          "원인·통과는 s5(expect_unknown)에서 통과 판정에 unknown_ok를 쓴다. 원인 열은 s5도 cause_ok 그대로 센다.", ""]
    L += _table(["항목", *(f"{c} {s}" for c, s, _ in cols)],
                [(name, *(_frac(_sel(records, condition=c, setting=s), key) for c, s, _ in cols))
                 for name, key in ITEMS]
                + [("규칙 이름 되풀이 원인(R1~R4, 보고용: 호출 수/원인 수)",
                    *(_echo(_sel(records, condition=c, setting=s)) for c, s, _ in cols))])

    repro = [s for s in ("prod", "warm") if s in present] or ["prod"]
    L += ["", "## ③ 재현성 (같은 설정 3회의 글이 모두 같은 시나리오 수)", ""]
    if repro == ["prod", "warm"]:
        L += ["앞의 두 열은 prod(첫 호출만 캐시 상태가 다름), 뒤의 두 열은 warm(먼저 한 번 보낸 뒤 3회)이다.", ""]
    rows = []
    for c in conds:
        row = [c]
        for setting in repro:
            same = [i for i in ids if reproducible(_sel(records, scenario=i, condition=c, setting=setting))]
            row += [f"{len(same)}/{len(ids)}", ", ".join(same) or "-"]
        rows.append(row)
    head = ["조건", "같은 시나리오 수", "같았던 시나리오"]
    if repro == ["prod", "warm"]:
        head += ["warm 같은 시나리오 수", "warm 같았던 시나리오"]
    elif repro == ["warm"]:
        head = ["조건", "warm 같은 시나리오 수", "warm 같았던 시나리오"]
    L += _table(head, rows)

    L += ["", "## ④ 환각 목록 (걸린 금지 키워드, 지어낸 숫자)", ""]
    groups: dict = {}
    for r in records:
        sc = r["score"]
        if sc["forbidden_hits"] or sc["invented_numbers"]:
            key = (r["scenario"], r["condition"], tuple(sc["forbidden_hits"]), tuple(sc["invented_numbers"]))
            groups[key] = groups.get(key, 0) + 1
    if groups:
        L += _table(["시나리오", "조건", "금지 키워드", "지어낸 숫자", "횟수"],
                    [(s, c, ", ".join(f) or "-", ", ".join(n) or "-", cnt)
                     for (s, c, f, n), cnt in sorted(groups.items())])
    else:
        L.append("없음")
    L += ["", "지어낸 숫자는 보고용이며 통과 판정에 쓰지 않는다.", ""]

    L += ["## ⑤ 시간 (조건별 시나리오)", ""]
    rows = []
    for i in ids:
        for c in conds:
            rs = _sel(records, scenario=i, condition=c)
            walls = [r["stats"]["wall_ms"] for r in rs]
            fails = [r["fail_reason"] for r in rs if not r["ok"]]
            rows.append((i, c, len(rs), _fmt(_mean(walls)), _fmt(max(walls) if walls else None),
                         _fmt(_mean([r["stats"]["prompt_eval_count"] for r in rs])),
                         _fmt(_mean([r["stats"]["prompt_eval_cached_count"] for r in rs])),
                         _fmt(_mean([r["stats"]["eval_count"] for r in rs])),
                         _cache_by_setting(rs),
                         ", ".join(sorted(set(map(str, fails)))) or "-"))
    L += _table(["시나리오", "조건", "호출", "wall_ms 평균", "wall_ms 최대", "prompt 토큰 평균", "캐시 재사용 평균",
                 "출력 토큰 평균", "설정별 캐시 재사용 평균", "실패 이유"], rows)

    L += ["", "## ⑥ 사람이 볼 시트 (prod 첫 호출의 답)", ""]
    for sc in scenarios if ("prod" in present or not present) else ():
        for c in conds:
            first = next(iter(_sel(records, scenario=sc["id"], condition=c, setting="prod", rep=1)), None)
            L += [f"### {sc['id']} / {c}", f"- 제목: {sc['title']}", f"- 정답 설명: {sc['ground_truth']}"]
            if first is None:
                L.append("- (호출 기록 없음)")
            elif first["parsed"] is None or not first["parsed"]["ok"]:
                reason = first["fail_reason"] or "bad_output"
                L += [f"- (파싱 불가 또는 호출 실패: {reason})", f"- 원문: {first['text']!r}"]
            else:
                p = first["parsed"]
                L += [f"- 요약: {p['summary']}", "- 원인 후보:", *(f"  {n}. {t}" for n, t in enumerate(p["causes"], 1)),
                      "- 확인할 항목:", *(f"  {n}. {t}" for n, t in enumerate(p["checks"], 1))]
            if first is not None:
                sc_ = first["score"]
                strict = "통과" if sc_["strict_passed"] else "탈락"
                L.append(f"- 자동 판정: {'통과' if sc_['passed'] else '탈락'}"
                         f" (엄격 {strict}, 근거 없는 원인 {sc_['unsupported_causes']}개)")
            L += ["- 정확성 0~2: ____ / 유용성 0~2: ____", ""]
    if compare:
        L += _compare_tables(records, scenarios, conds)
    if any("score_recorded" in r for r in records):
        L += _recorded_diff(records, "⑪" if compare else "⑦")
    return "\n".join(L) + "\n"


def _compare_tables(records, scenarios, conds) -> list:
    """T6a 비교표 ⑦~⑩. warmup은 항상 빠진다(setting을 warm, var로 지정해서 센다)."""
    design = [s["id"] for s in scenarios if not s.get("holdout")]
    holdout = [s["id"] for s in scenarios if s.get("holdout")]
    counted = ("warm", "var")

    def pooled(c, ids, key):
        rows = [r for r in records if r["condition"] == c and r["scenario"] in ids and r["setting"] in counted]
        return _frac(rows, key) if ids else "-"

    L = ["", "## ⑦ 조건 x 시나리오 묶음 통과 수 (warm과 var 합침)", "",
         f"설계 시나리오 {len(design)}개, 보류 시나리오 {len(holdout)}개. "
         "v1 규칙은 passed, strict_v3는 엄격 v3 통과다.", ""]
    L += _table(["조건", "설계 passed", "설계 strict_v3", "보류 passed", "보류 strict_v3"],
                [(c, pooled(c, design, "passed"), pooled(c, design, "strict_v3_passed"),
                  pooled(c, holdout, "passed"), pooled(c, holdout, "strict_v3_passed")) for c in conds])

    L += ["", "## ⑧ 시나리오 x 조건 strict_v3 통과율 (warm 3회, var 5회)", ""]
    header = ["시나리오", *(f"{c} {s}" for c in conds for s in counted)]
    L += _table(header, [(sc["id"] + (" (보류)" if sc.get("holdout") else ""),
                          *(_frac(_sel(records, scenario=sc["id"], condition=c, setting=s), "strict_v3_passed")
                            for c in conds for s in counted)) for sc in scenarios])

    L += ["", "## ⑨ 조건별 prompt 토큰·시간 평균 (warmup 제외)", ""]
    rows = []
    for c in conds:
        rs = [r for r in records if r["condition"] == c and r["setting"] != "warmup"]
        rows.append((c, len(rs), _fmt(_mean([r.get("est_prompt_tokens") for r in rs])),
                     _fmt(_mean([r["stats"]["prompt_eval_count"] for r in rs])),
                     _fmt(_mean([r["stats"]["prompt_eval_cached_count"] for r in rs])),
                     _fmt(_mean([r["stats"]["wall_ms"] for r in rs]))))
    L += _table(["조건", "호출", "추정 토큰 평균", "prompt 토큰 평균", "캐시 재사용 평균", "wall_ms 평균"], rows)

    L += ["", "## ⑩ 사람이 볼 시트 (보류 시나리오 x 조건, warm 1번 호출의 답. 자동 판정은 적지 않음)", ""]
    for sc in scenarios:
        if not sc.get("holdout"):
            continue
        for c in conds:
            first = next(iter(_sel(records, scenario=sc["id"], condition=c, setting="warm", rep=1)), None)
            L += [f"### {sc['id']} / {c}", f"- 제목: {sc['title']}", f"- 정답 설명: {sc['ground_truth']}"]
            if first is None:
                L.append("- (호출 기록 없음)")
            elif first["parsed"] is None or not first["parsed"]["ok"]:
                L += [f"- (파싱 불가 또는 호출 실패: {first['fail_reason'] or 'bad_output'})",
                      f"- 원문: {first['text']!r}"]
            else:
                p = first["parsed"]
                L += [f"- 요약: {p['summary']}", "- 원인 후보:", *(f"  {n}. {t}" for n, t in enumerate(p["causes"], 1)),
                      "- 확인할 항목:", *(f"  {n}. {t}" for n, t in enumerate(p["checks"], 1))]
            L += ["- 정확성 0~2: ____ / 유용성 0~2: ____", ""]
    return L


def _cache_by_setting(rows) -> str:
    """설정별 prompt_eval_cached_count 평균(예: 'prod 120 / warm 340')."""
    parts = []
    for st in SETTING_NAMES:
        vals = [r["stats"]["prompt_eval_cached_count"] for r in rows if r["setting"] == st]
        if vals:
            parts.append(f"{st} {_fmt(_mean(vals))}")
    return " / ".join(parts) or "-"


def changed_from_recorded(records) -> list:
    """원본 raw에 기록된 점수(score_recorded)와 지금 채점의 passed가 다른 호출."""
    return [r for r in records if "score_recorded" in r and r["score_recorded"]["passed"] != r["score"]["passed"]]


def _recorded_diff(records, label: str = "⑦") -> list:
    old = sum(1 for r in records if r["score_recorded"]["passed"])
    new = sum(1 for r in records if r["score"]["passed"])
    strict = sum(1 for r in records if r["score"]["strict_passed"])
    changed = changed_from_recorded(records)
    L = [f"## {label} 원본 기록 대비 변화", "",
         f"원본에 기록된 통과 {old}/{len(records)}, 지금 채점 통과 {new}/{len(records)}, "
         f"엄격 통과 {strict}/{len(records)}. 통과 판정이 달라진 호출 {len(changed)}번.", ""]
    if changed:
        L += _table(["시나리오", "조건", "설정", "기록", "지금", "걸린 금지 키워드"],
                    [(r["scenario"], r["condition"], f"{r['setting']}{r['rep']}", r["score_recorded"]["passed"],
                      r["score"]["passed"], ", ".join(r["score"]["forbidden_hits"]) or "-") for r in changed])
        L.append("")
    return L


# ---------------------------------------------------------------- 실행 진입점

def _dry(scenarios, runbooks_text, out, conditions=CONDITIONS, blocks=None) -> None:
    """Ollama를 부르지 않고 프롬프트를 보여 준다.
    system 글은 (조건, system 내용)마다 한 번만 전문을 보이고 시나리오에는 sha256 앞부분만 적는다
    (match 조건은 시나리오마다 system이 다를 수 있다)."""
    shown: set = set()
    for sc in scenarios:
        for cond in conditions:
            prompt, mode = condition_spec(cond)
            rb = runbook_text_for(mode, sc, runbooks_text, blocks)
            _, messages, window = build_prompt(sc, rb, NOW_TS, prompt)
            system, user = messages[0]["content"], messages[1]["content"]
            out(f"===== {sc['id']} / {cond} =====")
            out(f"추정 토큰 {estimate_prompt_tokens(messages)} (system {len(system)} + user {len(user)}), "
                f"창 통계 requests={window.requests} 5xx={window.errors_5xx} "
                f"p95={window.p95_ms:.0f} rpm={window.rpm:.0f}, "
                f"system sha256={_sha256(system)[:12]}")
            if (cond, system) not in shown:
                shown.add((cond, system))
                out(f"--- system ({cond}, sha256={_sha256(system)[:12]}, 같은 system을 쓰는 시나리오는 생략) ---")
                out(system)
            out("--- user ---")
            out(user)
            out("")


def _summary_lines(records, scenarios) -> list:
    present = {r["setting"] for r in records}
    names = [st for st, _ in REPORT_SETTINGS if st in present or (not present and st in ("prod", "var"))]
    lines = []
    for sc in scenarios:
        cells = []
        for c in _conditions(records):
            parts = [f"{st} {_frac(_sel(records, scenario=sc['id'], condition=c, setting=st))}"
                     f"({_frac(_sel(records, scenario=sc['id'], condition=c, setting=st), 'strict_passed')})"
                     for st in names]
            cells.append(f"{c} " + " ".join(parts))
        lines.append(f"{sc['id']:<18} " + " | ".join(cells))
    return lines


def _read_raw(path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def rescore(rows, scenarios, runbooks_text, blocks=None) -> list:
    """raw 기록의 응답 글을 다시 parse_answer와 score_answer로 채점한다. Ollama를 부르지 않는다.
    근거·런북 글은 build_prompt로 다시 만든다(시나리오가 같으면 결정적). 원본 점수는 score_recorded에 남긴다."""
    by_id = {sc["id"]: sc for sc in scenarios}
    out_rows = []
    for row in rows:
        sc = by_id.get(row["scenario"])
        if sc is None:
            raise ValueError(f"raw의 시나리오를 찾을 수 없다: {row['scenario']}")
        prompt, mode = condition_spec(row["condition"])
        rb = runbook_text_for(mode, sc, runbooks_text, blocks)
        _, messages, _ = build_prompt(sc, rb, NOW_TS, prompt)
        parsed = analysis.parse_answer(row["text"]) if row["ok"] else None
        out_rows.append({**row, "score_recorded": row["score"], "parsed": _parsed_dict(parsed),
                         "score": scoring.score_answer(sc, parsed, messages[1]["content"], rb)})
    return out_rows


def _first_options(rows, setting) -> dict:
    return next((r["options"] for r in rows if r["setting"] == setting), {})


def main(argv=None, *, chat=llm_client.chat, version=llm_client.version, out=print, now=None,
         settings=SETTINGS, scenario_dirs=(SCENARIO_DIR,)) -> int:
    ap = argparse.ArgumentParser(description="장애 분석 채점표 실행기 (A1-T5a, T5b)")
    ap.add_argument("--dry", action="store_true", help="Ollama를 부르지 않고 프롬프트와 추정 토큰만 출력")
    ap.add_argument("--out", default=str(REPO_ROOT / "docs" / "eval"), help="결과 폴더")
    ap.add_argument("--only", default="", help="쉼표로 구분한 시나리오(예: s1,s2)")
    ap.add_argument("--model", default=None, help="기본은 watcher 설정의 모델")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--scenarios", action="append", default=None, help="시나리오 폴더(여러 번 가능)")
    ap.add_argument("--runbooks", default=str(REPO_ROOT / "watcher" / "runbooks"))
    ap.add_argument("--keywords", choices=scoring.KEYWORD_VERSIONS, default="v1", help="채점 키워드 버전")
    ap.add_argument("--rescore", metavar="RAW", default=None, help="Ollama 없이 raw jsonl을 다시 채점한다")
    ap.add_argument("--conditions", default=None,
                    help="쉼표로 구분한 조건(기본 no_runbook,runbook = nr_v1,all_v1의 별칭). "
                         f"가능: {', '.join(variants.CONDITIONS)}")
    ap.add_argument("--settings", default=None, help="쉼표로 구분한 설정(예: warm,var). 기본 전체, warm은 warmup 포함")
    ap.add_argument("--holdout", default=str(HOLDOUT_DIR), help="보류 시나리오 폴더(--with-holdout일 때만 쓴다)")
    ap.add_argument("--with-holdout", action="store_true", help="보류 시나리오(s8~s11)를 시나리오에 합친다")
    args = ap.parse_args(argv)

    try:
        dirs = list(args.scenarios or scenario_dirs)
        if args.with_holdout:
            if not Path(args.holdout).is_dir():
                raise ValueError(f"보류 시나리오 폴더가 없다: {args.holdout}")
            dirs.append(args.holdout)
        scenarios = load_scenarios(dirs, [o for o in args.only.split(",") if o], args.keywords)
        conditions = parse_conditions(args.conditions)
        if args.settings:
            settings = parse_settings(args.settings, settings)
        llm_client.validate_base_url(args.url)
    except ValueError as e:
        out(f"인자 오류: {e}")
        return 2
    runbooks_text = analysis.load_runbooks(args.runbooks)
    blocks = variants.load_runbook_blocks(args.runbooks)
    if args.dry:
        _dry(scenarios, runbooks_text, out, conditions, blocks)
        return 0

    started = now or datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    base_meta = {"started_utc": stamp, "prompt_version": analysis.PROMPT_VERSION,
                 "system_sha256": _sha256(analysis.SYSTEM_PROMPT_V1), "runbook_chars": len(runbooks_text),
                 "keywords": args.keywords, "aborted": False}
    if args.conditions or args.with_holdout:     # T6a 비교 실행이면 점수판에 비교표 ⑦~⑩을 더한다
        base_meta.update(compare=True, system_sha256_v2=_sha256(variants.SYSTEM_PROMPT_V2),
                         title="# 장애 분석 비교 채점표 (A1-T6a)")

    if args.rescore:
        return _main_rescore(args, scenarios, runbooks_text, blocks, base_meta, out_dir, stamp, out)

    cfg = config.Config()
    model = args.model or cfg.llm_model
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    raw_path, board_path = out_dir / "raw" / f"eval-{stamp}.jsonl", out_dir / f"scoreboard-{stamp}.md"

    meta = {**base_meta, "model": model, "ollama_version": version(args.url) or "알 수 없음",
            "options_prod": dispatcher.options_from(cfg).as_dict(),
            "options_var": {**dispatcher.options_from(cfg, temperature=0.2).as_dict(), "seed": "1~5"},
            "keep_alive": cfg.llm_keep_alive, "connect_timeout": cfg.llm_connect_timeout,
            "total_timeout": cfg.llm_timeout}

    done: list = []
    with raw_path.open("w", encoding="utf-8") as raw:
        def save(rec):
            raw.write(json.dumps({"run_id": stamp, **rec}, ensure_ascii=False) + "\n")
            raw.flush()
            out(f"[{len(done) + 1:>3}] {rec['scenario']} {rec['condition']} {rec['setting']}{rec['rep']} "
                f"ok={rec['ok']} passed={rec['score']['passed']} strict={rec['score']['strict_passed']} "
                f"wall={rec['stats']['wall_ms']:.0f}ms" + ("" if rec["ok"] else f" 실패={rec['fail_reason']}"))
            done.append(rec)
        records, aborted = run_all(scenarios, runbooks_text, chat=chat, url=args.url, model=model, cfg=cfg,
                                   keep_alive=cfg.llm_keep_alive, connect_timeout=cfg.llm_connect_timeout,
                                   total_timeout=cfg.llm_timeout, on_record=save, settings=settings,
                                   conditions=conditions, blocks=blocks)
    meta["aborted"] = aborted
    board_path.write_text(render_scoreboard(meta, records, scenarios), encoding="utf-8")

    counted = [r for r in records if r["setting"] != "warmup"]
    out("")
    out("\n".join(_summary_lines(records, scenarios)))
    passed = sum(1 for r in counted if r["score"]["passed"])
    strict = sum(1 for r in counted if r["score"]["strict_passed"])
    skipped = f"(warmup {len(records) - len(counted)}번은 집계에서 뺌)" if len(counted) != len(records) else ""
    out(f"합계 {len(records)}번 호출, 통과 {passed}{skipped}, 엄격 통과 {strict}, "
        f"실패 호출 {sum(1 for r in records if not r['ok'])}")
    out(f"결과: {raw_path}\n      {board_path}")
    if aborted:
        out(f"중단: 연속 {MAX_CONSECUTIVE_FAILS}번 실패")
        return 2
    return 0


def _main_rescore(args, scenarios, runbooks_text, blocks, base_meta, out_dir, stamp, out) -> int:
    """--rescore: raw를 읽어 다시 채점하고 scoreboard-rescore-<UTC>.md만 만든다(raw는 만들지 않는다)."""
    try:
        rows = _read_raw(args.rescore)
        used = {r["scenario"] for r in rows}
        scenarios = [sc for sc in scenarios if sc["id"] in used]
        records = rescore(rows, scenarios, runbooks_text, blocks)
    except (OSError, ValueError, KeyError) as e:
        out(f"재채점 실패: {e}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    board_path = out_dir / f"scoreboard-rescore-{stamp}.md"
    meta = {**base_meta, "title": base_meta.get("title", "# 장애 분석 재채점표 (A1-T5b)"),
            "model": rows[0]["model"] if rows else "-",
            "ollama_version": "(재채점은 Ollama를 부르지 않는다. 원본 실행의 값은 원본 점수판 참고)",
            "options_prod": _first_options(rows, "prod"),
            "options_var": {**_first_options(rows, "var"), "seed": "1~5"},
            "source_raw": str(args.rescore)}
    board_path.write_text(render_scoreboard(meta, records, scenarios), encoding="utf-8")

    counted = [r for r in records if r["setting"] != "warmup"]
    changed = changed_from_recorded(records)
    out("\n".join(_summary_lines(records, scenarios)))
    out(f"재채점 {len(records)}행(키워드 {args.keywords}): 통과 {sum(1 for r in counted if r['score']['passed'])}, "
        f"엄격 통과 {sum(1 for r in counted if r['score']['strict_passed'])}, "
        f"원본 기록 대비 통과 판정이 달라진 호출 {len(changed)}")
    out(f"결과: {board_path}")
    if args.keywords == "v1" and changed:
        out("불일치: v1 재채점이 원본 기록의 통과 판정과 다르다")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(settings=SETTINGS_V2, scenario_dirs=(SCENARIO_DIR, EXTRA_SCENARIO_DIR)))
