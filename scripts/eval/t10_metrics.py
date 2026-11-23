#!/usr/bin/env python3
"""T10 판정 지표 계산기. raw jsonl을 읽어 사전 등록된 기준 1~3, 5의 숫자와 비교표 자료를 표로 출력한다.
판정 글은 쓰지 않는다(판정은 PM이 한다). 기준 4(사람 채점)는 이 도구가 다루지 않는다. 표준 라이브러리만 쓴다.

    python3 scripts/eval/t10_metrics.py docs/eval/raw/eval-<UTC>.jsonl

표본: (시나리오, 조건)마다 warm의 첫 호출(rep 1) 1개 + var 5개 = 6개. warm 3번은 같은 답이라 하나만 센다.
호출 실패나 형식 오류 표본은 causes가 비어 있다고 보고 분모에는 넣는다(실패 표본 수는 따로 적는다).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_WORDS = ("DB", "db", "데이터베이스")
S_NONDB = ("s12_new_exception", "s15_timeout_error", "s16_real_error_shape", "s18_new_exception_report",
           "s19_real_shape_other_path")
S_DB = ("s2_db_down", "s8_db_partial_fail", "s17_db_new_path")
BASE, TEST = "all_v1", "chunk_v1"


def load_truth(path=HERE / "truth_tokens.json") -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_raw(path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def samples(rows) -> dict:
    """{(시나리오, 조건): [표본 기록...]} warm rep 1 하나와 var 전부(최대 6개)."""
    out: dict = {}
    for r in rows:
        if (r["setting"] == "warm" and r["rep"] == 1) or r["setting"] == "var":
            out.setdefault((r["scenario"], r["condition"]), []).append(r)
    return out


def causes_of(rec) -> list:
    p = rec.get("parsed")
    return list(p["causes"]) if p and p.get("ok") else []


def db_in_causes(rec) -> bool:
    return any(w in c for c in causes_of(rec) for w in DB_WORDS)


def first_cause_ok(rec, tokens) -> bool:
    cs = causes_of(rec)
    return bool(cs) and any(t in cs[0] for t in tokens)


def ratio(num, den) -> str:
    return f"{num}/{den} ({100 * num / den:.1f}%)" if den else "-"


def _pct(num, den):
    return 100 * num / den if den else None


def table(header, rows) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def pooled(samp, cond, scenarios, fn) -> tuple:
    """(참인 표본 수, 표본 수)."""
    recs = [r for s in scenarios for r in samp.get((s, cond), [])]
    return sum(1 for r in recs if fn(r)), len(recs)


def report(rows, truth) -> str:
    samp = samples(rows)
    conds = list(dict.fromkeys(r["condition"] for r in rows))
    scs = list(dict.fromkeys(r["scenario"] for r in rows))
    L = ["# T10 판정 지표 (판정은 PM이 한다)", ""]
    failed = {c: sum(1 for k, v in samp.items() if k[1] == c for r in v if not (r.get("parsed") or {}).get("ok"))
              for c in conds}
    L += ["표본 = warm 첫 호출 1 + var 5. 형식 실패·호출 실패 표본(causes 없음으로 계산): "
          + ", ".join(f"{c} {n}" for c, n in failed.items()), ""]

    def crit(title, scenarios, fn, note):
        L.extend([f"## {title}", note, ""])
        rws = []
        for c in conds:
            n, d = pooled(samp, c, scenarios, fn)
            rws.append((c, ratio(n, d)))
        L.append(table(["조건", "비율"], rws))
        a, t = pooled(samp, BASE, scenarios, fn), pooled(samp, TEST, scenarios, fn)
        pa, pt = _pct(*a), _pct(*t)
        if pa is not None and pt is not None:
            L.append(f"\n{TEST} {pt:.1f}% / {BASE} {pa:.1f}%  (차이 {pt - pa:+.1f}%p, "
                     f"{TEST}/{BASE} 비 {pt / pa:.2f})" if pa else
                     f"\n{TEST} {pt:.1f}% / {BASE} 0.0%  (차이 {pt - pa:+.1f}%p)")
        L.append("")

    crit("기준1 DB 환각률 (S_nonDB 5개, db_in_causes)", S_NONDB, db_in_causes,
         "기준: chunk_v1이 all_v1의 절반 이하. 대상: " + ", ".join(S_NONDB))
    crit("기준2 DB 검출률 (S_DB 3개, db_in_causes)", S_DB, db_in_causes,
         "기준: chunk_v1이 all_v1보다 25%p 넘게 낮지 않음. 대상: " + ", ".join(S_DB))
    tr = tuple(truth)       # 기준3은 시나리오마다 토큰이 달라 아래에서 따로 계산
    L += ["## 기준3 첫 원인 정답률 (truth_tokens 6개, first_cause_ok)",
          "기준: chunk_v1이 all_v1보다 10%p 넘게 낮지 않음. 대상: " + ", ".join(tr), ""]
    rws, res = [], {}
    for c in conds:
        recs = [(r, truth[s]) for s in tr for r in samp.get((s, c), [])]
        n = sum(1 for r, tk in recs if first_cause_ok(r, tk))
        res[c] = _pct(n, len(recs))
        rws.append((c, ratio(n, len(recs))))
    L.append(table(["조건", "비율"], rws))
    if res.get(BASE) is not None and res.get(TEST) is not None:
        L.append(f"\n{TEST} {res[TEST]:.1f}% / {BASE} {res[BASE]:.1f}%  (차이 {res[TEST] - res[BASE]:+.1f}%p)")
    L += ["", "시나리오별 first_cause_ok:", ""]
    L.append(table(["시나리오", *conds],
                   [(s, *(ratio(sum(1 for r in samp.get((s, c), []) if first_cause_ok(r, truth[s])),
                                len(samp.get((s, c), []))) for c in conds)) for s in tr]))

    L += ["", "## 기준5 시나리오별 strict_v3 통과율 (6표본) — 기준: all_v1 대비 50%p 이상 하락한 시나리오가 없음", ""]

    def sv3(s, c):
        recs = samp.get((s, c), [])
        return sum(1 for r in recs if r["score"]["strict_v3_passed"]), len(recs)
    rws = []
    for s in scs:
        cells = [ratio(*sv3(s, c)) for c in conds]
        a, t = _pct(*sv3(s, BASE)), _pct(*sv3(s, TEST))
        rws.append((s, *cells, "-" if a is None or t is None else f"{t - a:+.1f}"))
    L.append(table(["시나리오", *conds, f"{TEST}-{BASE} (%p)"], rws))

    L += ["", "## 비교표 자료: 조건 x 시나리오 db_in_causes 비율", ""]
    L.append(table(["시나리오", *conds],
                   [(s, *(ratio(*pooled(samp, c, [s], db_in_causes)) for c in conds)) for s in scs]))
    L += ["", "## 비교표 자료: 조건 x 시나리오 strict_v3 통과율", ""]
    L.append(table(["시나리오", *conds], [(s, *(ratio(*sv3(s, c)) for c in conds)) for s in scs]))
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("사용법: t10_metrics.py <raw jsonl>")
        return 2
    print(report(read_raw(argv[0]), load_truth()), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
