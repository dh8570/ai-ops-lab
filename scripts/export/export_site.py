#!/usr/bin/env python3
"""공개 내보내기 조립 (T8d). 표준 라이브러리만 쓴다.

허용 목록에 있는 기록·문서·그림만 모아 정적 사이트(dist/ 아래)를 만든다. 안전 판단은 safety.py와 mdhtml.py가
하고, 이 파일은 그것들을 올바른 순서로 연결하는 일만 한다.

    python3 scripts/export/export_site.py --allowlist FILE --runs-src DIR --out dist/site
        [--force] [--list] [--from-dir DIR]

종료 코드: 0 성공, 1 검사 실패, 2 사용법 오류.

만드는 순서(실패하면 --out은 건드리지 않는다)
  1. 허용 목록 검사 -> 기록마다 (원본 검증 -> 공개 검사 -> 사본 검증) -> 문서·그림 변환 -> 전부 메모리에 모은다
  2. dist 안의 임시 폴더에 쓴다 -> safety.check_output_tree로 폴더 전체를 마지막으로 검사한다
  3. 통과하면 --out으로 옮긴다. 하나라도 문제면 문제 목록(값 없음)만 출력하고 임시 폴더를 지운다.
원칙: 오류 메시지에는 파일, 줄 번호, 패턴 이름만 쓰고 기록·문서의 내용(값)은 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import mdhtml
import safety
from safety import ExportError

REPO = Path(__file__).resolve().parents[2]            # scripts/export/export_site.py -> 레포 루트 (검증 코드의 위치)
STATIC_PREFIX = "explorer/static/"
APP_PY = "explorer/app.py"
MAX_ALLOWLIST = 1024 * 1024
FIGURE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+\.png")   # 그림 파일 이름: 주소에 그대로 쓸 수 있는 글자만
SLUG_RE = re.compile(r"[A-Za-z0-9-]+")
# 보고서 페이지의 CSP. 스크립트·연결은 아예 막고, 같은 곳의 CSS와 그림만 허용한다.
REPORT_CSP = "default-src 'none'; img-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'"
LIST_COLUMNS = ("run_id", "kind", "created_at", "status", "model", "prompt_version", "incident")


class SourceError(Exception):
    """소스(git 또는 폴더)에서 읽지 못함. 메시지에는 값이 아닌 경로만 쓴다."""


# ---------------------------------------------------------------- 소스: read(path)->bytes, tracked()->set, head()->str
class DirSource:
    """폴더에서 읽는 소스(--from-dir과 시험용). 폴더 안의 파일(심볼릭 링크 포함)을 모두 '추적됨'으로 본다."""

    def __init__(self, root):
        self.root = Path(root)

    def read(self, path: str) -> bytes:
        try:
            return (self.root / path).read_bytes()
        except OSError as e:
            raise SourceError(f"{path}: 읽지 못함 ({type(e).__name__})") from None

    def tracked(self) -> set[str]:
        found = set()
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) == self.root and d in (".git", "dist"))]
            for name in (*filenames, *(d for d in dirnames if (Path(dirpath) / d).is_symlink())):
                found.add((Path(dirpath) / name).relative_to(self.root).as_posix())
        return found

    def head(self) -> str:
        return "dir-source"


def _run_git(args: list[str]) -> bytes:
    """git을 목록 인자로 부른다(shell을 쓰지 않는다). 실패하면 SourceError."""
    p = subprocess.run(args, capture_output=True, check=False)  # noqa: S603  (인자는 목록, shell=False)
    if p.returncode != 0:
        raise SourceError(f"git {' '.join(args[3:5])} 실패 (종료 코드 {p.returncode})")
    return p.stdout


class GitSource:
    """git의 HEAD에 커밋된 내용을 읽는 소스. 작업 트리의 고치다 만 파일은 읽지 않는다.
    run은 git 실행기(args 목록 -> 표준 출력 bytes)이며 시험에서 가짜로 바꿔 끼운다."""

    def __init__(self, root, run=None):
        self.root = Path(root)
        self._run = run or _run_git

    def _git(self, *args: str) -> bytes:
        return self._run(["git", "-C", str(self.root), *args])

    def read(self, path: str) -> bytes:
        return self._git("show", f"HEAD:{path}")      # "HEAD:" 접두사가 있어 path가 옵션으로 읽힐 수 없다

    def tracked(self) -> set[str]:
        return {p.decode("utf-8") for p in self._git("ls-files", "-z").split(b"\0") if p}

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").decode("ascii").strip()


# ---------------------------------------------------------------- 검증 코드 불러오기
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_validators():
    """(watcher의 llm_trace, explorer의 runs). 폴더 전체를 import 경로에 올리지 않고 파일 하나씩만 읽는다
    (두 폴더에 이름이 같은 모듈이 있어도 섞이지 않게)."""
    for need in (REPO / "watcher" / "llm_client.py", REPO / "watcher" / "llm_trace.py", REPO / "explorer" / "runs.py"):
        if not need.is_file():
            raise SourceError(f"{need.relative_to(REPO).as_posix()}: 검증 코드가 없음")
    if "llm_client" not in sys.modules:
        _load_module("llm_client", REPO / "watcher" / "llm_client.py")     # llm_trace가 import한다(표준 라이브러리만)
    trace = _load_module("export_llm_trace", REPO / "watcher" / "llm_trace.py")
    runs = _load_module("export_explorer_runs", REPO / "explorer" / "runs.py")
    return trace, runs


# ---------------------------------------------------------------- 작은 도구
def _clean(msg: str) -> str:
    """검증기 메시지에서 따옴표 안의 값(repr)을 지운다. 기록 안의 글이 메시지로 새지 않게 한다."""
    msg = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", str(msg))
    msg = re.sub(r"[\x00-\x1f\x7f]", " ", msg)
    return msg[:160]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(obj, indent=2) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=indent) + "\n").encode("utf-8")


def csp_from_app(app_py: str) -> str:
    """explorer/app.py의 Content-Security-Policy 문자열에서 frame-ancestors(meta로는 쓸 수 없음)를 뺀 것."""
    for node in ast.walk(ast.parse(app_py)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == "Content-Security-Policy"):
            policy = ast.literal_eval(node.value)
            kept = [d.strip() for d in policy.split(";") if d.strip() and d.split()[0].lower() != "frame-ancestors"]
            csp = "; ".join(kept)
            if not csp or any(c in csp for c in '"<>&'):
                raise ValueError("CSP 문자열을 meta에 넣을 수 없는 모양")
            return csp
    raise ValueError("app.py에서 Content-Security-Policy를 찾지 못함")


def with_csp(page: bytes, csp: str) -> bytes:
    """<head> 바로 뒤에 CSP meta 한 줄을 넣는다. <head>\\n가 정확히 한 번 있어야 한다."""
    marker = b"<head>\n"
    if page.count(marker) != 1:
        raise ValueError("<head> 줄이 정확히 한 번 있어야 함")
    meta = f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'.encode()
    return page.replace(marker, marker + meta, 1)


def _label(text: str) -> str:
    """마크다운 링크 글자로 쓸 수 있게 대괄호와 줄바꿈을 없앤다."""
    return re.sub(r"[\[\]\r\n]+", " ", text).strip()


# ---------------------------------------------------------------- 기록 입력
def _read_limited(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SourceError(f"{path.name}: 일반 파일이 아님")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise SourceError(f"{path.name}: 파일이 너무 큼")
    return path.read_bytes()


def read_incident_lines(runs_src: Path):
    """llm_runs.jsonl.1(옛것)과 llm_runs.jsonl의 줄들. (기록 dict, 줄 위치 글) 목록과 읽지 못한 줄 목록."""
    entries, bad = [], []
    for fname in ("llm_runs.jsonl.1", "llm_runs.jsonl"):
        path = runs_src / fname
        if not path.exists():
            continue
        for no, raw in enumerate(_read_limited(path).split(b"\n"), start=1):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                bad.append(f"{fname} 줄 {no}: JSON으로 읽지 못함")
                continue
            if not isinstance(rec, dict):
                bad.append(f"{fname} 줄 {no}: 객체가 아님")
                continue
            entries.append((rec, f"{fname}:{no}"))
    return entries, bad


def read_playground_file(runs_src: Path, run_id: str):
    path = runs_src / "runs" / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        rec = json.loads(_read_limited(path).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SourceError(f"runs/{run_id}.json: JSON으로 읽지 못함") from None
    return rec


# ---------------------------------------------------------------- --list
def _cell(v) -> str:
    s = "-" if v is None else str(v)
    return re.sub(r"[\x00-\x1f\x7f]", " ", s)[:40]


def list_records(runs_src: Path, out) -> int:
    """기록 목록만 표로 낸다. 요약 글·근거 글·모델 답은 읽어도 내지 않는다."""
    rows, problems = [], []
    entries, bad = read_incident_lines(runs_src)
    problems += bad
    pg_dir = runs_src / "runs"
    pg = []
    if pg_dir.is_dir():
        for p in sorted(pg_dir.glob("*.json")):
            try:
                pg.append((json.loads(_read_limited(p).decode("utf-8")), f"runs/{p.name}"))
            except (SourceError, UnicodeDecodeError, ValueError):
                problems.append(f"runs/{p.name}: 읽지 못함")
    for rec, _where in entries + pg:
        if not isinstance(rec, dict):
            continue
        env = rec.get("environment") if isinstance(rec.get("environment"), dict) else {}
        inc = rec.get("incident") if isinstance(rec.get("incident"), dict) else {}
        rows.append((rec.get("run_id"), rec.get("kind"), rec.get("created_at"), rec.get("status"),
                     env.get("model"), env.get("prompt_version"), inc.get("id")))
    table = [LIST_COLUMNS, *[tuple(_cell(c) for c in r) for r in rows]]
    widths = [max(len(r[i]) for r in table) for i in range(len(LIST_COLUMNS))]
    for r in table:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip(), file=out)
    print(f"기록 {len(rows)}개", file=out)
    for p in problems:
        print(f"경고: {p}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 조립
def build_records(allow: dict, runs_src: Path, deny: list, trace, runs_mod, problems: list):
    """허용된 기록을 검증·공개 검사해 {run_id: (공개 기록, 입력 위치)}로 돌려준다. 문제는 problems에 더한다."""
    entries, _bad = read_incident_lines(runs_src)         # 허용되지 않은 줄은 공개와 무관하므로 오류로 보지 않는다
    by_id: dict[str, list] = {}
    for rec, where in entries:
        if isinstance(rec.get("run_id"), str):
            by_id.setdefault(rec["run_id"], []).append((rec, where))
    done = {}
    for i, item in enumerate(allow.get("runs", [])):
        rid, src = item["run_id"], item["source"]                 # validate_allowlist가 이미 모양을 확인했다
        if src == "incident":
            found = by_id.get(rid, [])
            if len(found) > 1:
                problems.append(f"runs[{i}]: {rid}가 runs-src에 {len(found)}번 있음")
                continue
            rec, where = found[0] if found else (None, None)
            check = trace.validate_record
        else:
            try:
                rec = read_playground_file(runs_src, rid)
            except SourceError as e:
                problems.append(f"runs[{i}]: {e}")
                continue
            where, check = f"runs/{rid}.json", runs_mod.validate_bundle
        if rec is None:
            problems.append(f"runs[{i}]: {rid}가 runs-src에 없음")
            continue
        errs = check(rec)
        if isinstance(rec, dict) and rec.get("kind") != src:
            errs = [*errs, "kind가 허용 목록의 source와 다름"]
        if errs:
            problems += [f"{rid}: 원본 검증 실패: {_clean(e)}" for e in errs[:5]]
            continue
        try:
            pub = safety.prepare_public_record(rec, deny)
        except ExportError as e:
            problems += e.problems
            continue
        errs = runs_mod.validate_bundle(pub)
        if errs:
            problems += [f"{rid}: 공개 사본 검증 실패: {_clean(e)}" for e in errs[:5]]
            continue
        done[rid] = (pub, where)
    return done


def build_docs(allow: dict, source, deny: list, problems: list):
    """문서를 읽어 reports/<slug>.html로 바꾼다. [(slug, title, 경로, html bytes)]를 돌려준다."""
    pages, slugs = [], {}
    for n, item in enumerate(allow.get("docs", []), start=1):
        path, title = item["path"], item["title"]
        stem = PurePosixPath(path).name[:-3]                      # .md를 뗀다
        slug = stem if SLUG_RE.fullmatch(stem) else f"doc-{n}"
        if slug in slugs:
            problems.append(f"docs[{n - 1}]: 쪽 이름 충돌 ({slug})")
            continue
        slugs[slug] = path
        try:
            text = source.read(path).decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: UTF-8 글이 아님")
            continue
        except SourceError as e:
            problems.append(str(e))
            continue
        # 변환 결과는 check_output_tree가 또 검사하지만, html.escape가 따옴표 등을 바꿔 패턴을 가릴 수 있어 원문도 본다.
        problems += [str(f) for f in safety.scan_text(text, path, deny)]
        pages.append((slug, title, path, with_csp(mdhtml.render_page(title, text, "../css/site.css").encode("utf-8"),
                                                  REPORT_CSP)))
    return pages


def build_figures(allow: dict, source, problems: list):
    """그림을 읽어 PNG를 정리한다. [(파일 이름, alt, 경로, bytes)]와 지운 청크 이름 수를 돌려준다."""
    figs, seen, removed_total = [], {}, 0
    for i, item in enumerate(allow.get("figures", [])):
        path = item["path"]
        name = PurePosixPath(path).name
        if not FIGURE_NAME_RE.fullmatch(name):
            problems.append(f"figures[{i}]: 파일 이름이 [A-Za-z0-9._-]로 된 .png가 아님")
            continue
        if name in seen:
            problems.append(f"figures[{i}]: 파일 이름 충돌 ({name})")
            continue
        seen[name] = path
        try:
            data, removed = safety.sanitize_png(source.read(path))
        except (ExportError, SourceError) as e:
            problems.append(f"{path}: {_clean(e)}")
            continue
        removed_total += len(removed)
        figs.append((name, item["alt"], path, data))
    return figs, removed_total


def reports_index_md(pages, figs) -> str:
    lines = ["# 자료 모음", "", "## 문서", ""]
    lines += [f"- [{_label(t)}](./{slug}.html)" for slug, t, _p, _h in pages] or ["- (없음)"]
    lines += ["", "## 그림", ""]
    lines += [f"- [{_label(alt)}](../figures/{name})" for name, alt, _p, _d in figs] or ["- (없음)"]
    return "\n".join(lines) + "\n"


def assemble(allow: dict, allow_bytes: bytes, source, runs_src: Path):
    """사이트의 모든 파일을 메모리에 모은다. ({경로: (bytes, 출처)}, 건수 dict)를 돌려준다. 문제는 ExportError."""
    trace, runs_mod = load_validators()
    deny = list(allow.get("deny_extra", []))
    problems: list[str] = []
    files: dict[str, tuple[bytes, str]] = {}

    def put(rel: str, data: bytes, origin: str):
        if rel in files:
            problems.append(f"{rel}: 같은 경로를 두 번 만듦")
        elif rel.startswith(("/", "../")) or ".." in rel.split("/") or rel == "":
            problems.append("허용되지 않은 출력 경로")
        else:
            files[rel] = (data, origin)

    # 1) 기록
    records = build_records(allow, runs_src, deny, trace, runs_mod, problems)
    ordered = sorted(records.values(), key=lambda pw: (pw[0]["created_at"], pw[0]["run_id"]), reverse=True)
    for pub, where in ordered:
        put(f"data/runs/{pub['run_id']}.json", _json_bytes(pub), f"runs-src:{where}")
    put("data/index.json", _json_bytes([runs_mod.index_entry(p) for p, _w in ordered], indent=1), "generated:index")

    # 2) explorer/static (index.html만 CSP meta 한 줄을 더한다)
    try:
        csp = csp_from_app(source.read(APP_PY).decode("utf-8"))
    except (ValueError, SyntaxError, UnicodeDecodeError, SourceError) as e:
        csp = None
        problems.append(f"{APP_PY}: CSP를 만들지 못함 ({type(e).__name__})")
    static = sorted(p for p in source.tracked() if p.startswith(STATIC_PREFIX))
    if not any(p == STATIC_PREFIX + "index.html" for p in static):
        problems.append(f"{STATIC_PREFIX}index.html이 추적되지 않음")
    for p in static:
        rel = p[len(STATIC_PREFIX):]
        if (Path(source.root) / p).is_symlink():
            problems.append(f"{p}: 심볼릭 링크")
            continue
        try:
            data = source.read(p)
            if rel == "index.html" and csp:
                data = with_csp(data, csp)
        except (SourceError, ValueError) as e:
            problems.append(f"{p}: 읽거나 CSP를 넣지 못함 ({type(e).__name__})")
            continue
        put(rel, data, p)

    # 3) 문서와 그림, 목록 쪽
    pages = build_docs(allow, source, deny, problems)
    figs, removed_total = build_figures(allow, source, problems)
    for slug, _t, path, html_bytes in pages:
        put(f"reports/{slug}.html", html_bytes, path)
    for name, _alt, path, data in figs:
        put(f"figures/{name}", data, path)
    index_html = mdhtml.render_page("자료 모음", reports_index_md(pages, figs), "../css/site.css")
    put("reports/index.html", with_csp(index_html.encode("utf-8"), REPORT_CSP), "generated:reports-index")
    put(".nojekyll", b"", "generated")
    if problems:
        raise ExportError(problems)

    counts = {"incident": sum(1 for p, _w in records.values() if p["kind"] == "incident"),
              "playground": sum(1 for p, _w in records.values() if p["kind"] == "playground"),
              "docs": len(pages), "figures": len(figs), "removed_chunks": removed_total}
    manifest = {"version": 1, "head": source.head(), "allowlist_sha256": _sha256(allow_bytes), "counts": counts,
                "files": [{"path": rel, "size": len(data), "sha256": _sha256(data), "source": origin}
                          for rel, (data, origin) in sorted(files.items())]}
    put("EXPORT_MANIFEST.json", _json_bytes(manifest), "generated")
    if problems:
        raise ExportError(problems)
    return files, counts


# ---------------------------------------------------------------- 쓰기와 마지막 검사
def write_checked(files: dict, out: Path, force: bool, deny: list) -> None:
    """dist 안의 임시 폴더에 쓰고 check_output_tree를 통과하면 out으로 옮긴다. 실패하면 임시 폴더를 지운다."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-export-", dir=out.parent))
    try:
        for rel, (data, _origin) in files.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        problems = safety.check_output_tree(tmp, set(files), extra_deny=deny)
        if problems:
            raise ExportError(problems)
        os.chmod(tmp, 0o755)
        if force and out.exists():
            shutil.rmtree(out)                                    # --force는 그 폴더만 지운다
        os.rename(tmp, out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 명령줄
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"사용법 오류: {message}", file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="export_site.py", description="허용 목록의 기록·문서·그림으로 공개용 정적 사이트를 만든다.")
    p.add_argument("--allowlist", help="허용 목록 JSON (--list에는 필요 없음)")
    p.add_argument("--runs-src", required=True, help="llm_runs.jsonl과 runs/<run_id>.json이 있는 폴더")
    p.add_argument("--out", help="출력 폴더. 레포의 dist/ 아래여야 함")
    p.add_argument("--force", action="store_true", help="이미 있는 --out 폴더를 지우고 다시 만듦")
    p.add_argument("--list", action="store_true", help="runs-src의 기록 목록만 출력(파일을 쓰지 않음)")
    p.add_argument("--from-dir", help="git 대신 이 폴더에서 읽음(시험용)")
    return p


def _usage_fail(msg: str) -> int:
    print(f"사용법 오류: {msg}", file=sys.stderr)
    return 2


def main(argv=None, git_runner=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    runs_src = Path(args.runs_src)
    if not runs_src.is_dir():
        return _usage_fail("--runs-src가 폴더가 아님")
    if args.list:
        try:
            return list_records(runs_src, sys.stdout)
        except SourceError as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
    if not args.allowlist or not args.out:
        return _usage_fail("--allowlist와 --out이 필요함")
    if args.from_dir:
        if not Path(args.from_dir).is_dir():
            return _usage_fail("--from-dir이 폴더가 아님")
        source = DirSource(args.from_dir)
    else:
        source = GitSource(REPO, git_runner)
    dist = (Path(source.root) / "dist").resolve()
    out = Path(args.out).resolve()
    if dist not in out.parents:
        return _usage_fail("--out은 레포의 dist/ 아래여야 함")
    if out.is_symlink() or out.exists():
        if not args.force:
            return _usage_fail("--out이 이미 있음 (다시 만들려면 --force)")
        if out.is_symlink() or not out.is_dir():
            return _usage_fail("--out이 폴더가 아님")
    try:
        allow_path = Path(args.allowlist)
        if allow_path.stat().st_size > MAX_ALLOWLIST:
            return _usage_fail("허용 목록이 너무 큼")
        allow_bytes = allow_path.read_bytes()
        allow = json.loads(allow_bytes.decode("utf-8"))
    except OSError:
        return _usage_fail("허용 목록 파일을 읽지 못함")
    except (UnicodeDecodeError, ValueError) as e:
        print(f"검사 실패: 허용 목록이 JSON이 아님 ({type(e).__name__})", file=sys.stderr)
        return 1
    try:
        problems = safety.validate_allowlist(allow, Path(source.root), source.tracked())
        if problems:
            raise ExportError(problems)
        files, counts = assemble(allow, allow_bytes, source, runs_src)
        write_checked(files, out, args.force, list(allow.get("deny_extra", [])))
    except ExportError as e:
        print(f"검사 실패: 문제 {len(e.problems)}건", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    except (SourceError, OSError) as e:
        print(f"오류: {_clean(e) if isinstance(e, SourceError) else type(e).__name__}", file=sys.stderr)
        return 1
    total = sum(len(d) for d, _o in files.values())
    print(f"기록 {counts['incident'] + counts['playground']}개(incident {counts['incident']}·playground "
          f"{counts['playground']}), 문서 {counts['docs']}개, 그림 {counts['figures']}개"
          f"(지운 청크 {counts['removed_chunks']}개), 전체 크기 {total}바이트")
    return 0


if __name__ == "__main__":
    sys.exit(main())
