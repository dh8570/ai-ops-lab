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

시나리오 숫자는 합성이며 실측이 아니다. 출력: <out>/raw/eval-<UTC>.jsonl(호출마다 한 줄), <out>/scoreboard-<UTC>.md.
종료 코드: 0 정상 / 2 연속 실패로 중단 또는 인자 오류
"""
import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
_WATCHER = REPO_ROOT / "watcher"
if _WATCHER.is_dir() and str(_WATCHER) not in sys.path:       # watcher 코드를 설치 없이 불러온다(수정하지 않는다)
    sys.path.insert(0, str(_WATCHER))

import scoring  # noqa: E402

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
CONDITIONS = ("no_runbook", "runbook")
# (설정 이름, 반복 번호, 옵션 덮어쓰기). prod는 운영 기본값 3번(재현성), var는 temperature 0.2에 seed 1~5.
SETTINGS = ([("prod", i, {}) for i in range(1, 4)]
            + [("var", i, {"temperature": 0.2, "seed": i}) for i in range(1, 6)])
MAX_CONSECUTIVE_FAILS = 5
SYNTHETIC_NOTE = "시나리오 숫자는 합성이며 실측이 아니다."
ITEMS = (("형식", "format_ok"), ("언어", "language_ok"), ("원인", "cause_ok"), ("확인", "check_ok"),
         ("환각 없음", "no_hallucination"), ("통과", "passed"))


# ---------------------------------------------------------------- 시나리오와 프롬프트

def load_scenarios(directory, only=None) -> list:
    """폴더의 *.json을 파일 이름순으로 읽는다. only(['s1','s2'])는 id 전체 또는 '_' 앞 접두어와 맞는 것만 남긴다."""
    scenarios = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(Path(directory).glob("*.json"))]
    if only:
        known = {s["id"] for s in scenarios}
        for o in only:
            if not any(i == o or i.startswith(o + "_") for i in known):
                raise ValueError(f"알 수 없는 시나리오: {o}")
        scenarios = [s for s in scenarios if any(s["id"] == o or s["id"].startswith(o + "_") for o in only)]
    return scenarios


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


def build_prompt(scenario: dict, runbooks_text: str, now_ts: float = NOW_TS):
    """(근거 dict, messages, 창 통계). 운영의 dispatcher가 쓰는 함수 그대로다."""
    records = expand_records(scenario, now_ts) if scenario["records_available"] else None
    window = stats.summarize(records or [], WINDOW_SEC, EXCLUDE_PATHS)
    evidence = analysis.build_evidence(
        make_incident(scenario, now_ts), window, records, scenario["baseline_rpm"], scenario["probe"], now_ts,
        window_sec=WINDOW_SEC, exclude_paths=EXCLUDE_PATHS, runbooks_text=runbooks_text,
        current_rules=scenario["current_rules"])
    return evidence, analysis.build_messages(evidence, runbooks_text), window


def estimate_prompt_tokens(messages: list) -> int:
    return sum(analysis.estimate_tokens(m["content"]) for m in messages)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 실행

def _call_stats(result) -> dict:
    return {"wall_ms": result.wall_ms, "prompt_eval_count": result.prompt_eval_count,
            "prompt_eval_cached_count": result.prompt_eval_cached_count, "eval_count": result.eval_count,
            "load_ms": result.load_ms, "prompt_eval_ms": result.prompt_eval_ms, "eval_ms": result.eval_ms}


def run_all(scenarios, runbooks_text, *, chat, url, model, cfg, keep_alive, connect_timeout, total_timeout,
            now_ts: float = NOW_TS, on_record=None):
    """시나리오 → 조건 → 설정 순으로 호출한다(같은 조건의 호출이 연달아 가고, 호출은 서로 독립 요청이다).
    실패해도 이유를 기록하고 계속하며, 연속 MAX_CONSECUTIVE_FAILS번 실패하면 멈춘다.
    (기록 목록, 중단 여부)를 돌려준다."""
    records: list = []
    streak = 0
    for sc in scenarios:
        for cond in CONDITIONS:
            rb = runbooks_text if cond == "runbook" else ""
            _, messages, _ = build_prompt(sc, rb, now_ts)
            system, evidence_text = messages[0]["content"], messages[1]["content"]
            for setting, rep, override in SETTINGS:
                options = dispatcher.options_from(cfg, **override)
                result = chat(url, model, messages, options, fmt=analysis.ANALYSIS_SCHEMA, keep_alive=keep_alive,
                              connect_timeout=connect_timeout, total_timeout=total_timeout)
                parsed = analysis.parse_answer(result.text) if result.ok else None
                rec = {"scenario": sc["id"], "condition": cond, "setting": setting, "rep": rep,
                       "model": model, "options": options.as_dict(), "system_sha256": _sha256(system),
                       "est_prompt_tokens": estimate_prompt_tokens(messages),
                       "ok": result.ok, "fail_reason": result.reason, "done_reason": result.done_reason,
                       "http_status": result.http_status, "text": result.text,
                       "parsed": None if parsed is None else {
                           "ok": parsed.ok, "reason": parsed.reason, "summary": parsed.summary,
                           "causes": parsed.causes, "checks": parsed.checks, "flags": parsed.flags},
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


def reproducible(rows) -> bool:
    """prod 호출 3번이 모두 성공이고 응답 글이 같은가."""
    return len(rows) == 3 and all(r["ok"] for r in rows) and len({r["text"] for r in rows}) == 1


def render_scoreboard(meta: dict, records: list, scenarios: list) -> str:
    ids = [s["id"] for s in scenarios]
    L: list = ["# 장애 분석 채점표 (A1-T5a)", ""]
    if meta.get("aborted"):
        L += [f"> 주의: 연속 {MAX_CONSECUTIVE_FAILS}번 실패로 중단된 실행이다. 아래는 중단 전까지의 결과다.", ""]
    L += ["## 조건", ""]
    L += _table(["항목", "값"], [
        ("실행 시각(UTC)", meta["started_utc"]), ("모델", meta["model"]), ("Ollama 버전", meta["ollama_version"]),
        ("PROMPT_VERSION", meta["prompt_version"]), ("SYSTEM_PROMPT_V1 sha256", meta["system_sha256"]),
        ("옵션 7개 (prod x3)", json.dumps(meta["options_prod"])),
        ("옵션 7개 (var x5, seed 1~5)", json.dumps(meta["options_var"])),
        ("keep_alive / 연결·총 제한(초)",
         f"{meta['keep_alive']} / {meta['connect_timeout']:g}·{meta['total_timeout']:g}"),
        ("런북", f"{meta['runbook_chars']}자 (조건 runbook에서만 사용)"),
        ("호출 수", f"{len(records)} (시나리오 {len(ids)} x 조건 2 x 8)"),
        ("주의", SYNTHETIC_NOTE),
    ])

    cols = [(c, s, n) for c in CONDITIONS for s, n in (("prod", 3), ("var", 5))]
    L += ["", "## ① 시나리오 x 조건 통과율", ""]
    L += _table(["시나리오", *(f"{c} {s}({n}회)" for c, s, n in cols)],
                [(i, *(_frac(_sel(records, scenario=i, condition=c, setting=s)) for c, s, _ in cols)) for i in ids]
                + [("합계", *(_frac(_sel(records, condition=c, setting=s)) for c, s, _ in cols))])

    L += ["", "## ② 항목별 통과 수 (전체 시나리오 합)", "",
          "원인·통과는 s5(expect_unknown)에서 통과 판정에 unknown_ok를 쓴다. 원인 열은 s5도 cause_ok 그대로 센다.", ""]
    L += _table(["항목", *(f"{c} {s}" for c, s, _ in cols)],
                [(name, *(_frac(_sel(records, condition=c, setting=s), key) for c, s, _ in cols))
                 for name, key in ITEMS])

    L += ["", "## ③ 재현성 (prod 3회의 글이 모두 같은 시나리오 수)", ""]
    rows = []
    for c in CONDITIONS:
        same = [i for i in ids if reproducible(_sel(records, scenario=i, condition=c, setting="prod"))]
        rows.append((c, f"{len(same)}/{len(ids)}", ", ".join(same) or "-"))
    L += _table(["조건", "같은 시나리오 수", "같았던 시나리오"], rows)

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
        for c in CONDITIONS:
            rs = _sel(records, scenario=i, condition=c)
            walls = [r["stats"]["wall_ms"] for r in rs]
            fails = [r["fail_reason"] for r in rs if not r["ok"]]
            rows.append((i, c, len(rs), _fmt(_mean(walls)), _fmt(max(walls) if walls else None),
                         _fmt(_mean([r["stats"]["prompt_eval_count"] for r in rs])),
                         _fmt(_mean([r["stats"]["prompt_eval_cached_count"] for r in rs])),
                         _fmt(_mean([r["stats"]["eval_count"] for r in rs])),
                         ", ".join(sorted(set(map(str, fails)))) or "-"))
    L += _table(["시나리오", "조건", "호출", "wall_ms 평균", "wall_ms 최대", "prompt 토큰 평균", "캐시 재사용 평균",
                 "출력 토큰 평균", "실패 이유"], rows)

    L += ["", "## ⑥ 사람이 볼 시트 (prod 첫 호출의 답)", ""]
    for sc in scenarios:
        for c in CONDITIONS:
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
                L.append(f"- 자동 판정: {'통과' if first['score']['passed'] else '탈락'}")
            L += ["- 정확성 0~2: ____ / 유용성 0~2: ____", ""]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 실행 진입점

def _dry(scenarios, runbooks_text, out) -> None:
    """Ollama를 부르지 않고 프롬프트를 보여 준다.
    system 글은 조건마다 한 번만 전문을 보이고 시나리오에는 sha256 앞부분만 적는다."""
    shown: set = set()
    for sc in scenarios:
        for cond in CONDITIONS:
            rb = runbooks_text if cond == "runbook" else ""
            _, messages, window = build_prompt(sc, rb)
            system, user = messages[0]["content"], messages[1]["content"]
            out(f"===== {sc['id']} / {cond} =====")
            out(f"추정 토큰 {estimate_prompt_tokens(messages)} (system {len(system)} + user {len(user)}), "
                f"창 통계 requests={window.requests} 5xx={window.errors_5xx} "
                f"p95={window.p95_ms:.0f} rpm={window.rpm:.0f}, "
                f"system sha256={_sha256(system)[:12]}")
            if cond not in shown:
                shown.add(cond)
                out(f"--- system ({cond}, 이 조건의 모든 시나리오에서 같음) ---")
                out(system)
            out("--- user ---")
            out(user)
            out("")


def _summary_lines(records, scenarios) -> list:
    lines = []
    for sc in scenarios:
        cells = []
        for c in CONDITIONS:
            prod, var = _sel(records, scenario=sc["id"], condition=c, setting="prod"), \
                _sel(records, scenario=sc["id"], condition=c, setting="var")
            cells.append(f"{c} prod {_frac(prod)} var {_frac(var)}")
        lines.append(f"{sc['id']:<16} " + " | ".join(cells))
    return lines


def main(argv=None, *, chat=llm_client.chat, version=llm_client.version, out=print, now=None) -> int:
    ap = argparse.ArgumentParser(description="장애 분석 채점표 실행기 (A1-T5a)")
    ap.add_argument("--dry", action="store_true", help="Ollama를 부르지 않고 프롬프트와 추정 토큰만 출력")
    ap.add_argument("--out", default=str(REPO_ROOT / "docs" / "eval"), help="결과 폴더")
    ap.add_argument("--only", default="", help="쉼표로 구분한 시나리오(예: s1,s2)")
    ap.add_argument("--model", default=None, help="기본은 watcher 설정의 모델")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--scenarios", default=str(HERE / "scenarios"))
    ap.add_argument("--runbooks", default=str(REPO_ROOT / "watcher" / "runbooks"))
    args = ap.parse_args(argv)

    try:
        scenarios = load_scenarios(args.scenarios, [o for o in args.only.split(",") if o])
        llm_client.validate_base_url(args.url)
    except ValueError as e:
        out(f"인자 오류: {e}")
        return 2
    runbooks_text = analysis.load_runbooks(args.runbooks)
    if args.dry:
        _dry(scenarios, runbooks_text, out)
        return 0

    cfg = config.Config()
    model = args.model or cfg.llm_model
    started = now or datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    raw_path, board_path = out_dir / "raw" / f"eval-{stamp}.jsonl", out_dir / f"scoreboard-{stamp}.md"

    meta = {"started_utc": stamp, "model": model, "ollama_version": version(args.url) or "알 수 없음",
            "prompt_version": analysis.PROMPT_VERSION, "system_sha256": _sha256(analysis.SYSTEM_PROMPT_V1),
            "options_prod": dispatcher.options_from(cfg).as_dict(),
            "options_var": {**dispatcher.options_from(cfg, temperature=0.2).as_dict(), "seed": "1~5"},
            "keep_alive": cfg.llm_keep_alive, "connect_timeout": cfg.llm_connect_timeout,
            "total_timeout": cfg.llm_timeout, "runbook_chars": len(runbooks_text), "aborted": False}

    done: list = []
    with raw_path.open("w", encoding="utf-8") as raw:
        def save(rec):
            raw.write(json.dumps({"run_id": stamp, **rec}, ensure_ascii=False) + "\n")
            raw.flush()
            out(f"[{len(done) + 1:>2}] {rec['scenario']} {rec['condition']} {rec['setting']}{rec['rep']} "
                f"ok={rec['ok']} passed={rec['score']['passed']} wall={rec['stats']['wall_ms']:.0f}ms"
                + ("" if rec["ok"] else f" 실패={rec['fail_reason']}"))
            done.append(rec)
        records, aborted = run_all(scenarios, runbooks_text, chat=chat, url=args.url, model=model, cfg=cfg,
                                   keep_alive=cfg.llm_keep_alive, connect_timeout=cfg.llm_connect_timeout,
                                   total_timeout=cfg.llm_timeout, on_record=save)
    meta["aborted"] = aborted
    board_path.write_text(render_scoreboard(meta, records, scenarios), encoding="utf-8")

    out("")
    out("\n".join(_summary_lines(records, scenarios)))
    out(f"합계 {len(records)}번 호출, 통과 {sum(1 for r in records if r['score']['passed'])}, "
        f"실패 호출 {sum(1 for r in records if not r['ok'])}")
    out(f"결과: {raw_path}\n      {board_path}")
    if aborted:
        out(f"중단: 연속 {MAX_CONSECUTIVE_FAILS}번 실패")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
