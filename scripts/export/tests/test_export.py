"""T8d: 공개 내보내기 조립(export_site.py) 시험.

가짜 레포(임시 폴더)와 시험 안에서 만든 합성 기록만 쓴다. 비밀 모양의 값은 조각을 이어 만든다.
실패 시험은 모두 종료 코드가 1 또는 2이고 --out이 만들어지지 않았으며 출력에 값이 없음을 확인한다.
"""
import copy
import hashlib
import http.server
import json
import shutil
import socketserver
import struct
import threading
import urllib.request
import zlib
from pathlib import Path

import pytest

import export_site
import safety

REPO = Path(__file__).resolve().parents[3]
INC_ID, PG_ID = "r-20261117-120000-abcd", "r-20261116-090000-0123"
MARK = "VALUE-MARKER"
FAKE_HOOK = "https://hooks.slack" + ".com/services/" + MARK       # 비밀 검사기가 이 시험 파일을 세지 않게 조각으로
SETTING_KEYS = ("temperature", "seed", "top_k", "top_p", "repeat_penalty", "num_predict", "num_ctx")


# ---------------------------------------------------------------- 재료
def incident(rid=INC_ID, created="2026-11-17T12:00:00.000Z", title="incident #1"):
    return {"schema_version": 1, "run_id": rid, "created_at": created, "kind": "incident", "title": title,
            "exportable": False, "redaction": {"applied": False, "note": "n"},
            "environment": {"ollama_version": None, "model": "m1", "prompt_version": "v1"},
            "settings": dict.fromkeys(SETTING_KEYS, 1), "status": "ok", "error": None,
            "stages": [{"id": "evidence", "status": "ok", "duration_ms": None, "evidence": {"*": "computed"},
                        "output": {"summary": "SUMMARY-MARKER"}, "notes": []}],
            "incident": {"id": 7, "trigger": "open"}}


def playground(rid=PG_ID, created="2026-11-16T09:00:00.000Z"):
    return {"schema_version": 1, "run_id": rid, "created_at": created, "kind": "playground", "title": "hi",
            "exportable": False, "redaction": {"applied": False, "note": "n"},
            "environment": {"ollama_version": "0", "model": "m2",
                            "tokenizer": {"repo": "r", "revision": "v", "sha256": "s"}},
            "settings": {"temperature": 0, "num_predict": 1, "num_ctx": 1}, "status": "ok", "error": None,
            "stages": [{"id": "input", "status": "ok", "duration_ms": None, "evidence": {"*": "input"},
                        "output": {"prompt": "p", "prompt_chars": 1, "prompt_bytes": 1}, "notes": []}]}


def png_with_text(data: bytes) -> bytes:
    """IHDR 바로 뒤에 tEXt 청크 하나를 끼운 PNG."""
    body = b"k\x00v"
    chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF)
    cut = 8 + 8 + 13 + 4
    return data[:cut] + chunk + data[cut:]


def allowlist(**over):
    base = {"version": 1,
            "runs": [{"source": "incident", "run_id": INC_ID, "why": "예시"},
                     {"source": "playground", "run_id": PG_ID, "why": "예시"}],
            "docs": [{"path": "docs/a-doc.md", "title": "첫 문서"}, {"path": "docs/한글.md", "title": "둘째 문서"}],
            "figures": [{"path": "docs/figures/fig5_roadmap.png", "alt": "로드맵", "png_text_reviewed": True}],
            "deny_extra": ["ZZ-DENY-WORD"]}
    base.update(over)
    return base


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "figures").mkdir(parents=True)
    (repo / "docs" / "a-doc.md").write_text("# 문서 A\n\n본문 하나.\n\n![그림](../figures/fig5_roadmap.png)\n",
                                            encoding="utf-8")
    (repo / "docs" / "한글.md").write_text("# 문서 B\n\n- 항목\n", encoding="utf-8")
    real_png = (REPO / "docs" / "figures" / "fig5_roadmap.png").read_bytes()
    (repo / "docs" / "figures" / "fig5_roadmap.png").write_bytes(png_with_text(real_png))
    (repo / "explorer").mkdir()
    shutil.copytree(REPO / "explorer" / "static", repo / "explorer" / "static")
    shutil.copy(REPO / "explorer" / "app.py", repo / "explorer" / "app.py")
    src = tmp_path / "runs-src"
    (src / "runs").mkdir(parents=True)
    (src / "llm_runs.jsonl").write_text(json.dumps(incident(), ensure_ascii=False) + "\n", encoding="utf-8")
    (src / "runs" / f"{PG_ID}.json").write_text(json.dumps(playground()), encoding="utf-8")
    return {"repo": repo, "src": src, "tmp": tmp_path}


def run(world, capsys, allow=None, out="dist/site", extra=(), allow_name="allow.json"):
    """main을 부르고 (종료 코드, 표준 출력+표준 오류 글)을 돌려준다."""
    allow_path = world["tmp"] / allow_name
    allow_path.write_text(json.dumps(allowlist() if allow is None else allow, ensure_ascii=False), encoding="utf-8")
    out_arg = out if Path(out).is_absolute() else str(world["repo"] / out)
    code = export_site.main(["--allowlist", str(allow_path), "--runs-src", str(world["src"]), "--out", out_arg,
                             "--from-dir", str(world["repo"]), *extra])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def tree(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def build_ok(world, capsys, out="dist/site"):
    code, text = run(world, capsys, out=out)
    assert code == 0, text
    return world["repo"] / out, text


# ---------------------------------------------------------------- 정상 빌드
def test_build_ok_exact_file_set(world, capsys):
    out, text = build_ok(world, capsys)
    static = {p.relative_to(world["repo"] / "explorer" / "static").as_posix()
              for p in (world["repo"] / "explorer" / "static").rglob("*") if p.is_file()}
    expected = static | {"data/index.json", f"data/runs/{INC_ID}.json", f"data/runs/{PG_ID}.json",
                         "reports/index.html", "reports/a-doc.html", "reports/doc-2.html",
                         "figures/fig5_roadmap.png", ".nojekyll", "EXPORT_MANIFEST.json"}
    assert tree(out) == expected
    assert "기록 2개(incident 1·playground 1), 문서 2개, 그림 1개(지운 청크 1개)" in text
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".tmp-export-")]


def test_index_sorted_desc_with_entries(world, capsys):
    out, _ = build_ok(world, capsys)
    idx = json.loads((out / "data" / "index.json").read_text(encoding="utf-8"))
    assert [e["run_id"] for e in idx] == [INC_ID, PG_ID]                  # created_at 내림차순
    assert idx[0] == {"run_id": INC_ID, "created_at": "2026-11-17T12:00:00.000Z", "kind": "incident",
                      "title": "incident #1", "model": "m1", "status": "ok", "exportable": True}
    assert idx[1]["kind"] == "playground" and idx[1]["model"] == "m2"


def test_record_copies_public_and_original_untouched(world, capsys):
    before = (world["src"] / "llm_runs.jsonl").read_bytes()
    out, _ = build_ok(world, capsys)
    for rid in (INC_ID, PG_ID):
        rec = json.loads((out / "data" / "runs" / f"{rid}.json").read_text(encoding="utf-8"))
        assert rec["exportable"] is True
        assert rec["redaction"] == safety.PUBLIC_REDACTION
    assert (world["src"] / "llm_runs.jsonl").read_bytes() == before


def test_explorer_files_identical_except_index(world, capsys):
    out, _ = build_ok(world, capsys)
    static = world["repo"] / "explorer" / "static"
    for p in static.rglob("*"):
        if p.is_file() and p.relative_to(static).as_posix() != "index.html":
            assert (out / p.relative_to(static)).read_bytes() == p.read_bytes()


def test_index_html_differs_only_by_one_csp_line(world, capsys):
    out, _ = build_ok(world, capsys)
    orig = (world["repo"] / "explorer" / "static" / "index.html").read_text(encoding="utf-8").split("\n")
    new = (out / "index.html").read_text(encoding="utf-8").split("\n")
    assert len(new) == len(orig) + 1
    at = orig.index("<head>") + 1                       # <head> 바로 뒤
    assert new[:at] == orig[:at]
    csp_line = new[at]
    assert csp_line.startswith('<meta http-equiv="Content-Security-Policy" content="')
    assert new[:at] + new[at + 1:] == orig
    assert "frame-ancestors" not in csp_line and "script-src 'self'" in csp_line and "default-src 'none'" in csp_line


def test_report_pages_have_csp(world, capsys):
    out, _ = build_ok(world, capsys)
    for name in ("index", "a-doc", "doc-2"):
        page = (out / "reports" / f"{name}.html").read_text(encoding="utf-8")
        assert f'content="{export_site.REPORT_CSP}"' in page
        assert page.count("Content-Security-Policy") == 1
    index = (out / "reports" / "index.html").read_text(encoding="utf-8")
    assert 'href="./a-doc.html"' in index and 'href="./doc-2.html"' in index
    assert 'href="../figures/fig5_roadmap.png"' in index


def test_figure_text_chunks_removed(world, capsys):
    out, text = build_ok(world, capsys)
    cleaned = (out / "figures" / "fig5_roadmap.png").read_bytes()
    assert b"tEXt" not in cleaned
    assert safety.sanitize_png(cleaned)[1] == []
    assert "지운 청크 1개" in text


def test_manifest_matches_files(world, capsys):
    out, _ = build_ok(world, capsys)
    man = json.loads((out / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    paths = {f["path"] for f in man["files"]}
    assert paths == tree(out) - {"EXPORT_MANIFEST.json"}
    for f in man["files"]:
        data = (out / f["path"]).read_bytes()
        assert f["size"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
        assert f["source"]
    allow_bytes = (world["tmp"] / "allow.json").read_bytes()
    assert man["allowlist_sha256"] == hashlib.sha256(allow_bytes).hexdigest()
    assert man["head"] == "dir-source"
    assert man["counts"] == {"incident": 1, "playground": 1, "docs": 2, "figures": 1, "removed_chunks": 1}


def test_same_input_same_manifest(world, capsys):
    a, _ = build_ok(world, capsys, out="dist/one")
    b, _ = build_ok(world, capsys, out="dist/two")
    assert (a / "EXPORT_MANIFEST.json").read_bytes() == (b / "EXPORT_MANIFEST.json").read_bytes()


def test_force_replaces_only_out(world, capsys):
    out, _ = build_ok(world, capsys)
    (out / "stale.txt").write_text("x", encoding="utf-8")
    sibling = world["repo"] / "dist" / "keep"
    sibling.mkdir()
    code, text = run(world, capsys, extra=["--force"])
    assert code == 0, text
    assert not (out / "stale.txt").exists() and sibling.is_dir()


# ---------------------------------------------------------------- 실패 경로
def assert_failed(world, code, text, codes=(1, 2), out="dist/site", forbidden=()):
    assert code in codes, text
    assert not (world["repo"] / out).exists()
    assert not [p for p in (world["repo"] / "dist").glob(".tmp-export-*")]
    for f in (MARK, "SUMMARY-MARKER", *forbidden):
        assert f not in text


def test_fail_pattern_in_record(world, capsys):
    (world["src"] / "llm_runs.jsonl").write_text(
        json.dumps(incident(title="see " + FAKE_HOOK), ensure_ascii=False) + "\n", encoding="utf-8")
    code, text = run(world, capsys)
    assert_failed(world, code, text, codes=(1,))
    assert "slack_webhook" in text and INC_ID in text


def test_fail_untracked_doc(world, capsys):
    (world["repo"] / "dist").mkdir()
    (world["repo"] / "dist" / "x.md").write_text("x", encoding="utf-8")        # DirSource는 dist/를 추적 밖으로 본다
    code, text = run(world, capsys, allow=allowlist(docs=[{"path": "dist/x.md", "title": "t"}]))
    assert_failed(world, code, text, codes=(1,))
    assert "추적하지 않는" in text


def test_fail_path_escape(world, capsys):
    code, text = run(world, capsys, allow=allowlist(docs=[{"path": "../x.md", "title": "t"}]))
    assert_failed(world, code, text, codes=(1,))
    assert "경로 탈출" in text


def test_fail_run_id_missing_in_runs_src(world, capsys):
    (world["src"] / "llm_runs.jsonl").write_text("", encoding="utf-8")
    code, text = run(world, capsys)
    assert_failed(world, code, text, codes=(1,))
    assert INC_ID in text and "없음" in text


def test_fail_deny_extra_word_in_doc(world, capsys):
    (world["repo"] / "docs" / "a-doc.md").write_text("# t\n\n여기 zz-deny-word 가 있다\n", encoding="utf-8")
    code, text = run(world, capsys)
    assert_failed(world, code, text, codes=(1,), forbidden=("zz-deny-word", "ZZ-DENY-WORD"))
    assert "deny_extra" in text


def test_fail_out_outside_dist(world, capsys):
    code, text = run(world, capsys, out=str(world["tmp"] / "elsewhere"))
    assert code == 2 and not (world["tmp"] / "elsewhere").exists()
    code, text = run(world, capsys, out="dist")                                 # dist 자체도 안 된다
    assert code == 2


def test_fail_out_exists_without_force(world, capsys):
    out, _ = build_ok(world, capsys)
    marker = out / "keep.txt"
    marker.write_text("x", encoding="utf-8")
    code, text = run(world, capsys)
    assert code == 2 and marker.exists() and "--force" in text


def test_fail_source_record_invalid(world, capsys):
    bad = incident()
    del bad["stages"]
    bad["status"] = MARK                                                          # 값이 메시지로 새지 않는지도 본다
    (world["src"] / "llm_runs.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
    code, text = run(world, capsys)
    assert_failed(world, code, text, codes=(1,))
    assert "원본 검증 실패" in text and "stages" in text


def test_fail_usage(world, capsys):
    assert export_site.main(["--runs-src", str(world["src"])]) == 2
    assert export_site.main(["--runs-src", str(world["tmp"] / "none"), "--list"]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------- 하위 경로에서 열기
def test_served_under_subpath(world, capsys):
    out, _ = build_ok(world, capsys)
    web = world["tmp"] / "web"
    shutil.copytree(out, web / "ai-ops-lab")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(web), **kw)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Quiet) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/ai-ops-lab/"
        try:
            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    return r.status, r.read()

            for path in ("index.html", "reports/index.html", "figures/fig5_roadmap.png"):
                assert get(path)[0] == 200
            for path in ("data/index.json", f"data/runs/{INC_ID}.json", "content/stages.json"):
                status, body = get(path)
                assert status == 200 and json.loads(body)
        finally:
            srv.shutdown()


# ---------------------------------------------------------------- --list
def test_list_prints_fields_only(world, capsys):
    code = export_site.main(["--runs-src", str(world["src"]), "--list"])
    text = capsys.readouterr().out
    assert code == 0
    header = text.splitlines()[0].split()
    assert header == ["run_id", "kind", "created_at", "status", "model", "prompt_version", "incident"]
    assert INC_ID in text and PG_ID in text and "m1" in text and "v1" in text
    assert "SUMMARY-MARKER" not in text and "incident #1" not in text
    assert "기록 2개" in text


def test_list_needs_no_allowlist_and_writes_nothing(world, capsys):
    before = tree(world["tmp"])
    assert export_site.main(["--runs-src", str(world["src"]), "--list"]) == 0
    capsys.readouterr()
    assert tree(world["tmp"]) == before


# ---------------------------------------------------------------- GitSource (가짜 git 실행기)
def test_git_source_reads_committed_content_not_worktree(world):
    committed = {"docs/a-doc.md": "# 커밋된 글\n".encode()}
    (world["repo"] / "docs" / "a-doc.md").write_text("# 작업 트리에서 고친 글\n", encoding="utf-8")
    calls = []

    def fake_git(args):
        calls.append(args)
        sub = args[3:]
        if sub == ["ls-files", "-z"]:
            return b"docs/a-doc.md\0explorer/static/index.html\0"
        if sub == ["show", "HEAD:docs/a-doc.md"]:
            return committed["docs/a-doc.md"]
        if sub == ["rev-parse", "HEAD"]:
            return b"0123456789abcdef0123456789abcdef01234567\n"
        raise AssertionError(f"뜻밖의 git 호출: {sub}")

    src = export_site.GitSource(world["repo"], fake_git)
    assert src.tracked() == {"docs/a-doc.md", "explorer/static/index.html"}
    assert src.read("docs/a-doc.md") == committed["docs/a-doc.md"]
    assert src.head() == "0123456789abcdef0123456789abcdef01234567"
    assert all(c[0] == "git" and isinstance(c, list) for c in calls)

    problems = []
    pages = export_site.build_docs({"docs": [{"path": "docs/a-doc.md", "title": "t"}]}, src, [], problems)
    assert problems == [] and "커밋된 글" in pages[0][3].decode() and "작업 트리" not in pages[0][3].decode()


def test_git_failure_is_source_error(world):
    def failing(args):
        raise export_site.SourceError("git show 실패")

    with pytest.raises(export_site.SourceError):
        export_site.GitSource(world["repo"], failing).read("docs/a-doc.md")


# ---------------------------------------------------------------- 작은 도구
def test_csp_from_app_drops_frame_ancestors():
    app = ('resp.headers["Content-Security-Policy"] = '
           '("default-src \'none\'; " "frame-ancestors \'none\'; img-src \'self\'")')
    assert export_site.csp_from_app(app) == "default-src 'none'; img-src 'self'"
    with pytest.raises(ValueError):
        export_site.csp_from_app("x = 1")


def test_with_csp_requires_single_head():
    with pytest.raises(ValueError):
        export_site.with_csp(b"<html></html>", "default-src 'none'")


def test_clean_hides_quoted_values():
    assert MARK not in export_site._clean(f"status: 허용되지 않은 값 '{MARK}'")
    assert MARK not in export_site._clean(f'x: "{MARK}"')


def test_original_record_validation_matches_runs_module():
    trace, runs_mod = export_site.load_validators()
    assert trace.validate_record(incident()) == [] and runs_mod.validate_bundle(playground()) == []
    pub = safety.prepare_public_record(incident())
    # exportable True는 사본 검증(runs)만 통과하고 원본 검증(llm_trace)은 거부한다
    assert trace.validate_record(pub) != [] and runs_mod.validate_bundle(pub) == []
    assert copy.deepcopy(pub)["exportable"] is True
