"""T8b: 공개 내보내기 안전 게이트(safety.py) 시험.

가짜 값만 쓴다. 토큰 모양의 가짜 값은 비밀 검사기가 진짜로 오인하지 않게 실행 중에 조각을 이어 만든다.
모든 실패 시험은 Finding repr과 ExportError 문자열에 일치한 값이 없는지도 확인한다.
"""
import copy
import json
import struct
import zlib
from pathlib import Path

import pytest

from safety import (
    PATTERNS,
    ExportError,
    Finding,
    check_output_tree,
    prepare_public_record,
    sanitize_png,
    scan_text,
    validate_allowlist,
)

REPO = Path(__file__).resolve().parents[3]
MARK = "SECRET-MARKER"

# ---------------------------------------------------------------- scan_text

HITS = {
    "slack_webhook": ["see https://hooks.slack" + ".com/services/" + MARK],
    "discord_webhook": ["https://discord" + ".com/api/webhooks/1/" + MARK,
                        "https://discordapp" + ".com/api/webhooks/2"],
    "slack_token": ["xox" + "b-" + "SECRETMARKER1"],
    "github_token": ["gh" + "p_" + "SECRETMARKERabcdef12", "github" + "_pat_" + "SECRETMARKER_abcdef12"],
    "private_key": ["-----BEGIN " + "RSA PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----"],
    "aws_key": ["AKIA" + "SECRETMARKER0000"],
    "abs_path": ["path /ho" + "me/someone/x", "/root/" + ".ssh", "(/Users/" + "me)"],
    "email": ["mail someone" + "@example.com"],
    "hostname": ["ssh controller1", "on ai-lab-gpu now", "old jn-controller1", "vm ai-lab."],
    "secret_assignment": ["password = SECRETMARKER", "API_KEY: 'SECRETMARKER9'", "token=abcdefgh1234"],
    # tailnet(100.64/10) 예는 조각으로 만든다. 레포 공개 정리 검사가 이 시험 파일을 세지 않게 하기 위해서다
    "ipv4": ["10.0.0.5:11434", "192.168.1.10", "100." + "64.1.2", "169.254.0.1", "8.8.8.8", "010.000.000.001"],
}
CLEAN = ["tokenize", "토큰화", "Ollama 0.40.2", "127.0.0.1:8000", "localhost", "ai-ops-lab", "docker compose logs app",
         "장애 알림에 분석이 붙는다.", "0.0.0.0", "999.1.1.1", "입력 token 수는 1,000", "/home", "user@localhost",
         "ai-lab-gpu-2", "my-ai-lab", "v1.2.3", "password: short"]


def _no_value(value: str, *objs) -> None:
    for o in objs:
        assert value not in repr(o) and value not in str(o)


def test_patterns_cover_spec():
    assert set(HITS) - {"ipv4"} == set(PATTERNS)


@pytest.mark.parametrize("name,sample", [(n, s) for n, ss in HITS.items() for s in ss])
def test_pattern_hits(name, sample):
    found = scan_text(f"앞줄\n{sample}\n뒷줄", "x.md")
    assert Finding("x.md", 2, name) in found
    _no_value(sample, found)


@pytest.mark.parametrize("sample", CLEAN)
def test_clean_text_not_flagged(sample):
    assert scan_text(sample) == []


def test_same_pattern_twice_in_line_counts_once_and_lines_are_numbered():
    found = scan_text("controller1 controller1\n\n10.0.0.1")
    assert found == [Finding("", 1, "hostname"), Finding("", 3, "ipv4")]


def test_invisible_and_fullwidth_variants_are_caught():
    assert {f.pattern for f in scan_text("control\u200bler1")} == {"hostname"}
    assert {f.pattern for f in scan_text("ｃｏｎｔｒｏｌｌｅｒ１")} == {"hostname"}


def test_deny_extra_case_insensitive_and_short_word_rejected():
    found = scan_text("Project NIGHTOWL 문서", "a.md", extra_deny=["nightowl"])
    assert found == [Finding("a.md", 1, "deny_extra")]
    _no_value("NIGHTOWL", found)
    with pytest.raises(ExportError) as e:
        scan_text("x", extra_deny=["ab"])
    _no_value("ab", e.value)                             # 짧은 금지어 값도 메시지에 없다


def test_finding_repr_has_no_value():
    f = scan_text("password=" + MARK + "XYZ")[0]
    assert repr(f) == "Finding(file='', line=1, pattern='secret_assignment')"
    _no_value(MARK, f)

# ---------------------------------------------------------------- sanitize_png


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    pos, out = 8, []
    while pos < len(data):
        n, t = struct.unpack(">I4s", data[pos:pos + 8])
        out.append((t, data[pos + 8:pos + 8 + n]))
        pos += 12 + n
    return out


IDAT = zlib.compress(b"\x00\x7f")
TINY = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + _chunk(b"tEXt", b"Comment\x00" + MARK.encode())
        + _chunk(b"iTXt", b"Author\x00\x00\x00\x00\x00" + MARK.encode())
        + _chunk(b"eXIf", b"MM\x00*" + MARK.encode()) + _chunk(b"IDAT", IDAT) + _chunk(b"IEND", b"") + MARK.encode())


def test_sanitize_png_strips_text_exif_and_trailing():
    out, removed = sanitize_png(TINY)
    assert removed == ["tEXt", "iTXt", "eXIf", "trailing"]
    assert [t for t, _ in _chunks(out)] == [b"IHDR", b"IDAT", b"IEND"]
    assert dict(_chunks(out))[b"IDAT"] == IDAT
    assert MARK.encode() not in out
    assert sanitize_png(out) == (out, [])               # 결과도 유효한 PNG(서명·CRC)이고 더 지울 것이 없다


def test_sanitize_png_rejects_non_png_and_bad_crc():
    with pytest.raises(ExportError):
        sanitize_png(b"GIF89a" + MARK.encode())
    broken = bytearray(TINY)
    broken[20] ^= 0xFF                                   # IHDR 본문(16~28번 바이트) 하나를 바꿔 CRC를 깨뜨린다
    with pytest.raises(ExportError) as e:
        sanitize_png(bytes(broken))
    _no_value(MARK, e.value)


def test_sanitize_png_rejects_unknown_critical_chunk_and_truncation():
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    bad = TINY[:8] + ihdr + _chunk(b"ZZZZ", b"") + _chunk(b"IEND", b"")
    with pytest.raises(ExportError):
        sanitize_png(bad)
    with pytest.raises(ExportError):
        sanitize_png(TINY[:40])


@pytest.mark.parametrize("png", sorted((REPO / "docs" / "figures").glob("*.png")), ids=lambda p: p.name)
def test_real_figures_sanitize_without_error(png):
    out, removed = sanitize_png(png.read_bytes())
    assert sanitize_png(out)[1] == []
    assert set(removed) <= {"caBX", "tEXt", "iTXt", "zTXt", "eXIf", "tIME", "trailing"}, removed

# ---------------------------------------------------------------- validate_allowlist


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "docs" / "f.png").write_bytes(TINY)
    (tmp_path / "docs" / "untracked.md").write_text("x", encoding="utf-8")
    (tmp_path / "docs" / "link.md").symlink_to(tmp_path / "docs" / "a.md")
    (tmp_path / "docs" / "linkdir").symlink_to(tmp_path / "docs", target_is_directory=True)
    (tmp_path.parent / "outside.md").write_text("x", encoding="utf-8")
    return tmp_path


TRACKED = {"docs/a.md", "docs/f.png", "docs/link.md", "docs/linkdir/a.md"}


def _ok():
    return {"version": 1,
            "runs": [{"source": "incident", "run_id": "r-20261117-120000-abcd", "why": "예시"}],
            "docs": [{"path": "docs/a.md", "title": "A"}],
            "figures": [{"path": "docs/f.png", "alt": "그림", "png_text_reviewed": True}],
            "deny_extra": ["nightowl"]}


def test_allowlist_ok(repo):
    assert validate_allowlist(_ok(), repo, TRACKED) == []


def _set(path, value):
    def f(o):
        cur = o
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
    return f


CASES = {
    "unknown_key": (_set(["extra"], 1), "모르는 키"),
    "no_version": (lambda o: o.pop("version"), "version"),
    "why_missing": (lambda o: o["runs"][0].pop("why"), "why"),
    "why_blank": (_set(["runs", 0, "why"], "  "), "why"),
    "escape": (_set(["docs", 0, "path"], "docs/../../outside.md"), "경로 탈출"),
    "absolute": (_set(["docs", 0, "path"], "/etc/passwd.md"), "절대 경로"),
    "symlink": (_set(["docs", 0, "path"], "docs/link.md"), "심볼릭 링크"),
    "symlink_dir": (_set(["docs", 0, "path"], "docs/linkdir/a.md"), "심볼릭 링크"),
    "untracked": (_set(["docs", 0, "path"], "docs/untracked.md"), "추적하지 않는"),
    "doc_ext": (_set(["docs", 0, "path"], "docs/f.png"), "확장자"),
    "fig_ext": (_set(["figures", 0, "path"], "docs/a.md"), "확장자"),
    "duplicate": (lambda o: o["docs"].append(dict(o["docs"][0])), "중복"),
    "dup_run": (lambda o: o["runs"].append(dict(o["runs"][0])), "중복"),
    "png_not_reviewed": (_set(["figures", 0, "png_text_reviewed"], False), "png_text_reviewed"),
    "bad_run_id": (_set(["runs", 0, "run_id"], "r-2026-x"), "run_id"),
    "bad_source": (_set(["runs", 0, "source"], "shell"), "source"),
    "short_deny": (_set(["deny_extra"], ["ab"]), "deny_extra[0]"),
    "item_extra_key": (_set(["docs", 0, "note"], "x"), "키는"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_allowlist_problems(repo, case):
    mutate, expect = CASES[case]
    obj = _ok()
    mutate(obj)
    problems = validate_allowlist(obj, repo, TRACKED)
    assert any(expect in p for p in problems), problems


def test_allowlist_message_has_no_deny_value(repo):
    obj = _ok()
    obj["deny_extra"] = ["ab"]
    obj["runs"][0]["run_id"] = MARK
    _no_value(MARK, validate_allowlist(obj, repo, TRACKED))
    assert all("'ab'" not in p for p in validate_allowlist(obj, repo, TRACKED))


def test_allowlist_example_is_valid(tmp_path):
    example = json.loads((Path(__file__).resolve().parents[1] / "allowlist.example.json").read_text(encoding="utf-8"))
    paths = [it["path"] for sec in ("docs", "figures") for it in example[sec]]
    for p in paths:                                      # 형식 검사: 같은 경로 모양의 임시 레포에서 통과해야 한다
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_bytes(b"x")
    assert validate_allowlist(example, tmp_path, set(paths)) == []
    fig = REPO / example["figures"][0]["path"]           # 그림은 컨테이너에도 마운트되어 실제로 있어야 한다
    assert fig.is_file()

# ---------------------------------------------------------------- prepare_public_record


def _record(**kw):
    rec = {"schema_version": 1, "run_id": "r-20261117-120000-abcd", "created_at": "2026-11-17T12:00:00.000Z",
           "kind": "incident", "title": "합성 장애", "exportable": False,
           "redaction": {"applied": False, "note": "합성"}, "status": "ok",
           "stages": [{"id": "prompt", "status": "ok", "output": {"text": "5xx 비율 40%, 경로 /checkout"}}]}
    rec.update(kw)
    return rec


def test_prepare_public_record_ok_and_original_untouched():
    rec = _record()
    before = copy.deepcopy(rec)
    out = prepare_public_record(rec)
    assert rec == before
    assert out["exportable"] is True and out["redaction"]["applied"] is False
    assert "마스킹은 하지 않았고" in out["redaction"]["note"]
    out["stages"][0]["output"]["text"] = "바꿈"                # 깊은 복사본이다
    assert rec == before


@pytest.mark.parametrize("value,pattern", [("controller1", "hostname"), ("10.0.0.5", "ipv4"),
                                           ("someone" + "@example.com", "email"), ("NightOwl", "deny_extra")])
def test_prepare_public_record_rejects_patterns_without_value(value, pattern):
    rec = _record(stages=[{"id": "prompt", "output": {"text": f"원인 {value}"}}])
    before = copy.deepcopy(rec)
    with pytest.raises(ExportError) as e:
        prepare_public_record(rec, extra_deny=["nightowl"])
    assert any(p.startswith(f"r-20261117-120000-abcd: {pattern} (줄 ") for p in e.value.problems), e.value.problems
    _no_value(value, e.value)
    assert rec == before


def test_prepare_public_record_rejects_aborted_and_bad_run_id():
    with pytest.raises(ExportError, match="aborted"):
        prepare_public_record(_record(status="aborted"))
    with pytest.raises(ExportError) as e:
        prepare_public_record(_record(run_id=MARK))
    _no_value(MARK, e.value)


def test_prepare_public_record_rejects_over_2mb():
    with pytest.raises(ExportError, match="크기"):
        prepare_public_record(_record(stages=[{"id": "x", "output": {"text": "가" * 700_000}}]))

# ---------------------------------------------------------------- check_output_tree

SVG_NS = "http://www.w3.org/2000/svg"


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    (root / "css").mkdir(parents=True)
    (root / "js").mkdir()
    (root / "runs").mkdir()
    (root / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="./css/site.css"><script src="./js/app.js"></script>\n'
        '<a href="#top">위</a> <a href="./runs/r.html?x=1#s">기록</a> <a href="#/play">놀이</a>\n', encoding="utf-8")
    (root / "runs" / "r.html").write_text('<a href="../index.html">처음</a> <img src="../fig.png">', encoding="utf-8")
    (root / "css" / "site.css").write_text("body { color: black; }\n", encoding="utf-8")
    (root / "js" / "app.js").write_text(f'const NS = "{SVG_NS}"; document.createElementNS(NS, "svg");\n',
                                        encoding="utf-8")
    (root / "data.json").write_text('{"a": 1}\n', encoding="utf-8")
    (root / "fig.png").write_bytes(sanitize_png(TINY)[0])
    (root / ".nojekyll").write_bytes(b"")
    return root


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_tree_ok(site):
    assert check_output_tree(site, _files(site)) == []


def test_tree_extra_and_missing(site):
    expected = _files(site)
    (site / "stray.txt").write_text("x", encoding="utf-8")
    probs = check_output_tree(site, expected | {"gone.json"})
    assert "stray.txt: 목록에 없는 파일" in probs and "gone.json: 목록에 있는데 없는 파일" in probs


def test_tree_symlink(site):
    expected = _files(site)
    (site / "ln.json").symlink_to(site / "data.json")
    (site / "lndir").symlink_to(site / "css", target_is_directory=True)
    probs = check_output_tree(site, expected)
    assert "ln.json: 심볼릭 링크" in probs and "lndir: 심볼릭 링크" in probs


def test_tree_size_limits(site):
    assert any("파일 크기 상한" in p for p in check_output_tree(site, _files(site), max_file=10))
    assert "전체 크기 상한 초과" in check_output_tree(site, _files(site), max_total=100)


@pytest.mark.parametrize("href,why", [
    ("https://example.com/" + MARK, "외부 주소"), ("http://example.com", "외부 주소"),
    ("//example.com/" + MARK, "프로토콜 상대"), ("javascript:alert(1)", "javascript:"),
    ("java&#x09;script:alert(1)", "javascript:"), ("JaVaScRiPt:x", "javascript:"),
    ("data:text/html,x", "data:"), ("mailto:x", "스킴"), ("/abs.html", "절대 경로"),
    ("./nope.html", "깨진 상대 링크"), ("../../outside.html", "root 밖"),
])
def test_tree_bad_links(site, href, why):
    (site / "index.html").write_text(f'<p>\n<a href="{href}">x</a></p>', encoding="utf-8")
    probs = check_output_tree(site, _files(site))
    assert any(f"href {why}" in p and "(줄 2)" in p for p in probs), probs
    _no_value(MARK, probs)


def test_tree_patterns_in_html_and_json(site):
    (site / "index.html").write_text("<p>ssh controller1</p>", encoding="utf-8")
    (site / "data.json").write_text('{"ip": "10.0.0.5"}', encoding="utf-8")
    probs = check_output_tree(site, _files(site))
    assert "index.html: hostname (줄 1)" in probs and "data.json: ipv4 (줄 1)" in probs
    _no_value("10.0.0.5", probs)


def test_tree_css_import_and_external_url(site):
    (site / "css" / "site.css").write_text('@import "x.css";\nb { background: url( "https://e.com/a.png") }',
                                           encoding="utf-8")
    probs = check_output_tree(site, _files(site))
    assert "css/site.css: CSS @import (줄 1)" in probs and "css/site.css: CSS 외부 url() (줄 2)" in probs


def test_tree_png_must_be_sanitized_and_unknown_type_rejected(site):
    (site / "fig.png").write_bytes(TINY)
    (site / "x.woff").write_bytes(b"x")
    probs = check_output_tree(site, _files(site))
    assert any(p.startswith("fig.png: 정리되지 않은 PNG 청크") for p in probs)
    assert "x.woff: 허용되지 않은 파일 형식" in probs


def test_tree_base_tag_and_meta_refresh(site):
    (site / "index.html").write_text('<base href="./">\n<meta http-equiv="refresh" content="0">', encoding="utf-8")
    probs = check_output_tree(site, _files(site))
    assert "index.html: base 태그 (줄 1)" in probs and "index.html: meta refresh (줄 2)" in probs


def test_real_explorer_static_has_no_problems():
    root = REPO / "explorer" / "static"
    assert check_output_tree(root, _files(root)) == []
