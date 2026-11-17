"""공개 내보내기(정적 사이트)의 안전 게이트 (T8b). 표준 라이브러리만 쓴다.

무엇이 공개 사이트로 나가는지를 막는 검사 모음이다.
- scan_text: 비밀·내부 정보처럼 보이는 글을 줄 단위로 찾는다.
- sanitize_png: PNG에서 글·메타데이터 청크와 IEND 뒤 바이트를 지운다.
- validate_allowlist: 사람이 정한 공개 허용 목록의 모양과 경로를 검사한다.
- prepare_public_record: 실행 기록 하나를 공개용으로 검사하고 표시를 바꾼 복사본을 만든다.
- check_output_tree: 조립이 끝난 출력 폴더 전체를 마지막으로 검사한다.

원칙: 일치한 값은 어디에도 담지 않는다. Finding·오류 메시지에는 파일, 줄 번호, 패턴 이름만 들어간다.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import posixpath
import re
import struct
import unicodedata
import urllib.parse
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


class ExportError(Exception):
    """공개 검사 실패. problems는 값이 들어 있지 않은 문제 설명 목록이다."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super().__init__("공개 검사 실패: " + "; ".join(self.problems))


@dataclass(frozen=True)
class Finding:
    """패턴이 걸린 위치. 일치한 값은 담지 않는다(repr·str에도 없다)."""
    file: str
    line: int
    pattern: str

    def __str__(self):
        return f"{self.file}: {self.pattern} (줄 {self.line})"


PATTERNS = {name: re.compile(rx) for name, rx in {
    "slack_webhook": r"hooks\.slack\.com",
    "discord_webhook": r"discord(app)?\.com/api/webhooks",
    "slack_token": r"xox[abprs]-[A-Za-z0-9-]{8,}",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "abs_path": r"(?<![\w/.])/(home|root|Users)/[\w.\-]+",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "hostname": r"(?<![\w-])(ai-lab-gpu|jn-controller1|controller1|ai-lab)(?![\w-])",
    "secret_assignment": r"(?i)\b(api[_-]?key|access[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*[\"']?"
                         r"[A-Za-z0-9/_+=.\-]{8,}",
}.items()}
IPV4_CANDIDATE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DENY_MIN_LEN = 3
# 눈에 보이지 않는 글자. 값 사이에 끼워 패턴을 피하는 것을 막으려고 검사 전에 지운 판도 함께 본다.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


def _ipv4_hit(candidate: str) -> bool:
    """각 자리가 0~255이고 루프백(127.*)·0.0.0.0이 아니면 걸린다(사설, tailnet, 링크 로컬, 공인 모두)."""
    try:
        nums = [int(p) for p in candidate.split(".")]
    except ValueError:
        return False
    if any(n > 255 for n in nums):
        return False
    ip = ipaddress.IPv4Address(".".join(str(n) for n in nums))   # 앞자리 0(010 등)도 숫자로 읽어 판정한다
    return not (ip.is_loopback or ip.is_unspecified)


def _check_deny(extra_deny) -> list[str]:
    words = []
    for i, w in enumerate(extra_deny):
        if not isinstance(w, str) or len(w.strip()) < DENY_MIN_LEN:
            raise ExportError([f"deny_extra[{i}]: 길이 {DENY_MIN_LEN} 이상 글이어야 함"])   # 값은 쓰지 않는다
        words.append(unicodedata.normalize("NFKC", w.strip()).casefold())
    return words


def _variants(line: str) -> set[str]:
    """원래 줄과, 보이지 않는 글자를 지우고 NFKC(전각→반각 등)로 바꾼 줄. 둘 중 하나라도 걸리면 걸린 것이다."""
    return {line, unicodedata.normalize("NFKC", _INVISIBLE.sub("", line))}


def scan_text(text: str, name: str = "", extra_deny=()) -> list[Finding]:
    """줄 단위로 모든 패턴과 사람이 정한 금지어(대소문자 무시)를 검사한다. 같은 줄의 같은 패턴은 한 건으로 센다."""
    deny = _check_deny(extra_deny)
    found: list[Finding] = []
    for no, line in enumerate(text.split("\n"), start=1):
        hits = set()
        for v in _variants(line):
            hits.update(pname for pname, rx in PATTERNS.items() if rx.search(v))
            if any(_ipv4_hit(m.group(0)) for m in IPV4_CANDIDATE.finditer(v)):
                hits.add("ipv4")
            folded = v.casefold()
            if any(w in folded for w in deny):
                hits.add("deny_extra")
        found.extend(Finding(name, no, p) for p in sorted(hits))
    return found


# ---------------------------------------------------------------- PNG

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {b"IHDR", b"PLTE", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"pHYs", b"IDAT", b"IEND"}


def sanitize_png(data: bytes) -> tuple[bytes, list[str]]:
    """서명과 청크 CRC를 확인하고 허용 청크만 남긴 새 PNG와 지운 청크 이름 목록을 돌려준다.
    IEND 뒤 바이트는 지우고 "trailing"으로 적는다.
    허용 목록에 없는 필수(critical) 청크는 지우면 그림이 깨지므로 오류."""
    if not isinstance(data, bytes | bytearray) or not data.startswith(PNG_SIGNATURE):
        raise ExportError(["PNG 서명이 아님"])
    out, removed, pos, first = [PNG_SIGNATURE], [], len(PNG_SIGNATURE), True
    while True:
        if pos + 8 > len(data):
            raise ExportError(["PNG가 IEND 전에 끝남"])
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise ExportError(["PNG 청크 길이가 파일보다 김"])
        if not re.fullmatch(rb"[A-Za-z]{4}", ctype):
            raise ExportError(["PNG 청크 이름이 올바르지 않음"])
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:end])
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise ExportError([f"PNG 청크 CRC 불일치: {ctype.decode('ascii')}"])
        if first and ctype != b"IHDR":
            raise ExportError(["PNG 첫 청크가 IHDR가 아님"])
        first = False
        if ctype in PNG_KEEP:
            out.append(data[pos:end])
        elif ctype[0:1].isupper():                       # 첫 글자가 대문자 = 필수 청크
            raise ExportError([f"허용되지 않은 필수 PNG 청크: {ctype.decode('ascii')}"])
        else:
            removed.append(ctype.decode("ascii"))
        pos = end
        if ctype == b"IEND":
            break
    if pos < len(data):
        removed.append("trailing")
    return b"".join(out), removed


# ---------------------------------------------------------------- 허용 목록

RUN_ID_RE = re.compile(r"^r-\d{8}-\d{6}-[0-9a-f]{4}$")      # explorer/runs.py의 RUN_ID_RE와 같은 식
ALLOW_KEYS = {"version", "runs", "docs", "figures", "deny_extra"}
ITEM_KEYS = {"runs": {"source", "run_id", "why"}, "docs": {"path", "title"},
             "figures": {"path", "alt", "png_text_reviewed"}}


def _nonempty(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _path_problem(p, ext: str, repo_root: Path, tracked) -> str | None:
    """레포 안쪽 상대 경로이고, 심볼릭 링크가 아니며, git이 추적하는 파일인지. 문제가 없으면 None."""
    if not _nonempty(p):
        return "path가 비어 있음"
    if "\\" in p or "\x00" in p or p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        return "절대 경로이거나 허용되지 않는 글자가 있음"
    parts = PurePosixPath(p).parts
    if ".." in parts or any(part in ("", ".") for part in p.split("/")):
        return "경로 탈출(..)이거나 정규화되지 않은 경로"
    if not p.endswith(ext):
        return f"확장자가 {ext}가 아님"
    root = Path(repo_root)
    cur = root
    for part in parts:                                    # 중간 폴더까지 포함해 심볼릭 링크가 하나도 없어야 한다
        cur = cur / part
        if cur.is_symlink():
            return "심볼릭 링크"
    try:
        cur.resolve().relative_to(root.resolve())
    except ValueError:
        return "레포 밖을 가리킴"
    if p not in tracked:
        return "git이 추적하지 않는 파일"
    if not cur.is_file():
        return "파일이 없음"
    return None


def validate_allowlist(obj, repo_root, tracked) -> list[str]:
    """허용 목록 검증. 문제 목록을 돌려주고 비어 있으면 통과. 메시지에는 항목 위치만 쓰고 값은 쓰지 않는다."""
    if not isinstance(obj, dict):
        return ["허용 목록은 객체여야 함"]
    problems = [f"모르는 키: {k!r}" if isinstance(k, str) and k.isascii() and len(k) <= 40 else "모르는 키"
                for k in sorted(set(obj) - ALLOW_KEYS, key=str)]
    if obj.get("version") != 1 or isinstance(obj.get("version"), bool):
        problems.append("version은 1이어야 함")
    tracked = set(tracked)
    for section in ("runs", "docs", "figures"):
        items = obj.get(section, [])
        if not isinstance(items, list):
            problems.append(f"{section}는 목록이어야 함")
            continue
        seen = set()
        for i, it in enumerate(items):
            where = f"{section}[{i}]"
            if not isinstance(it, dict):
                problems.append(f"{where}: 객체여야 함")
                continue
            if set(it) != ITEM_KEYS[section]:
                problems.append(f"{where}: 키는 {sorted(ITEM_KEYS[section])}여야 함")
            if section == "runs":
                if it.get("source") not in ("incident", "playground"):
                    problems.append(f"{where}.source: incident 또는 playground여야 함")
                rid = it.get("run_id")
                if not (isinstance(rid, str) and RUN_ID_RE.fullmatch(rid)):
                    problems.append(f"{where}.run_id: 형식이 올바르지 않음")
                if not _nonempty(it.get("why")):
                    problems.append(f"{where}.why: 비어 있지 않은 글이어야 함")
                key = rid
            else:
                ext = ".md" if section == "docs" else ".png"
                bad = _path_problem(it.get("path"), ext, repo_root, tracked)
                if bad:
                    problems.append(f"{where}.path: {bad}")
                label = "title" if section == "docs" else "alt"
                if not _nonempty(it.get(label)):
                    problems.append(f"{where}.{label}: 비어 있지 않은 글이어야 함")
                if section == "figures" and it.get("png_text_reviewed") is not True:
                    problems.append(f"{where}.png_text_reviewed: true여야 함(그림 속 글자를 사람이 확인)")
                key = it.get("path")
            if isinstance(key, str):
                if key in seen:
                    problems.append(f"{where}: 중복 항목")
                seen.add(key)
    deny = obj.get("deny_extra", [])
    if not isinstance(deny, list):
        problems.append("deny_extra는 목록이어야 함")
    else:
        for i, w in enumerate(deny):
            if not isinstance(w, str) or len(w.strip()) < DENY_MIN_LEN:
                problems.append(f"deny_extra[{i}]: 길이 {DENY_MIN_LEN} 이상 글이어야 함")
    return problems


# ---------------------------------------------------------------- 실행 기록

MAX_RECORD_BYTES = 2 * 1024 * 1024
PUBLIC_REDACTION = {"applied": False,
                    "note": "공개 검사 통과(허용 목록, 패턴 검사). "
                            "마스킹은 하지 않았고 원문 로그를 AI에 보내지 않는 방식이다"}


def prepare_public_record(record: dict, extra_deny=()) -> dict:
    """깊은 복사본을 검사해 통과하면 exportable=True와 공개용 redaction을 넣어 돌려준다. 원본은 바꾸지 않는다.
    실패하면 ExportError("<run_id>: <패턴 이름> (줄 N)" 형식, 값 없음)."""
    if not isinstance(record, dict):
        raise ExportError(["실행 기록은 객체여야 함"])
    rec = copy.deepcopy(record)
    rid = rec.get("run_id")
    ok_id = isinstance(rid, str) and RUN_ID_RE.fullmatch(rid) is not None
    label = rid if ok_id else "(잘못된 run_id)"          # 형식이 틀린 run_id는 무엇이 들었는지 모르므로 쓰지 않는다
    problems = [] if ok_id else [f"{label}: run_id 형식이 올바르지 않음"]
    if rec.get("status") == "aborted":
        problems.append(f"{label}: 중단된(aborted) 기록은 공개하지 않음")
    text = json.dumps(rec, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
        problems.append(f"{label}: 직렬화 크기가 {MAX_RECORD_BYTES}B를 넘음")
    else:
        problems += [f"{label}: {f.pattern} (줄 {f.line})" for f in scan_text(text, label, extra_deny)]
    if problems:
        raise ExportError(problems)
    rec["exportable"] = True
    rec["redaction"] = dict(PUBLIC_REDACTION)
    return rec


# ---------------------------------------------------------------- 출력 폴더

TEXT_EXT = {".html", ".htm", ".json", ".js", ".mjs", ".css", ".md", ".txt", ".svg", ".xml"}
LINK_ATTRS = {"href", "src", "action", "formaction", "poster", "xlink:href", "srcset"}
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:")
_CSS_IMPORT = re.compile(r"@import", re.I)
_CSS_EXT_URL = re.compile(r"""url\(\s*["']?\s*(?:https?:|//|\\\\)""", re.I)


class _LinkParser(HTMLParser):
    """HTML의 링크 속성(값은 &#..; 등이 풀린 상태)과 줄 번호를 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[int, str, str]] = []
        self.flags: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        if tag == "base":                                # 상대 경로의 기준을 바꾼다
            self.flags.append((line, "base 태그"))
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            self.flags.append((line, "meta refresh"))
        for k, v in attrs:
            if k in LINK_ATTRS and v is not None:
                vals = [c.strip().split(" ")[0] for c in v.split(",")] if k == "srcset" else [v]
                self.links.extend((line, k, x) for x in vals)

    handle_startendtag = handle_starttag


def _url_problem(value: str) -> str | None:
    """링크 값의 종류를 판정한다. 외부·위험 주소면 이유, 상대 경로면 "relative", 문제없으면 None."""
    v = re.sub(r"[\x00-\x20\x7f]", "", value).lower()   # 브라우저는 앞뒤 공백·제어 글자와 중간의 탭·줄바꿈을 무시한다
    if v == "" or v.startswith("#"):
        return None
    if "javascript:" in v:
        return "javascript: 주소"
    if "data:" in v:
        return "data: 주소"
    if v.startswith(("//", "\\\\", "/\\", "\\/")):
        return "프로토콜 상대 주소"
    if _SCHEME.match(v):
        return "외부 주소" if v.startswith(("http:", "https:")) else "스킴이 있는 주소"
    if v.startswith(("/", "\\")):
        return "절대 경로"
    return "relative"


def _resolve(rel_file: str, value: str, files: set[str]) -> str | None:
    """상대 링크가 root 안의 파일을 가리키는지. 쿼리·앵커는 뗀다. 문제가 없으면 None."""
    p = urllib.parse.unquote(re.split(r"[?#]", value.strip(), maxsplit=1)[0]).replace("\\", "/")
    if p == "":
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel_file), p))
    if target == ".." or target.startswith("../") or target.startswith("/"):
        return "root 밖을 가리키는 링크"
    if target in files or posixpath.normpath(posixpath.join(target, "index.html")) in files:
        return None
    return "깨진 상대 링크"


def check_output_tree(root, expected: set[str], max_file=10 * 1024 * 1024, max_total=50 * 1024 * 1024,
                      extra_deny=()) -> list[str]:
    """출력 폴더 마지막 검사. 문제 목록을 돌려준다(비면 통과). 메시지에 일치한 값·링크 주소는 쓰지 않는다."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return ["출력 폴더가 없거나 심볼릭 링크임"]
    problems: list[str] = []
    files: set[str] = set()
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                problems.append(f"{(Path(dirpath) / d).relative_to(root).as_posix()}: 심볼릭 링크")
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                problems.append(f"{rel}: 심볼릭 링크")
                continue
            files.add(rel)
            size = full.lstat().st_size
            total += size
            if size > max_file:
                problems.append(f"{rel}: 파일 크기 상한 초과")
    problems += [f"{p}: 목록에 없는 파일" for p in sorted(files - set(expected))]
    problems += [f"{p}: 목록에 있는데 없는 파일" for p in sorted(set(expected) - files)]
    if total > max_total:
        problems.append("전체 크기 상한 초과")
    for rel in sorted(files):
        if scan_text(rel, rel, extra_deny):              # 파일 이름 자체에 비밀이 들었을 때 (이름은 그대로 쓴다)
            problems.append(f"{rel}: 파일 이름에 금지 패턴")
        full = root / rel
        ext = PurePosixPath(rel).suffix.lower()
        if ext == ".png":                                 # 출력의 PNG는 이미 정리되어 있어야 한다
            try:
                _, removed = sanitize_png(full.read_bytes())
                if removed:
                    problems.append(f"{rel}: 정리되지 않은 PNG 청크 {sorted(set(removed))}")
            except ExportError as e:
                problems.append(f"{rel}: {'; '.join(e.problems)}")
            continue
        if ext not in TEXT_EXT:
            if ext:
                problems.append(f"{rel}: 허용되지 않은 파일 형식")
            continue                                      # 확장자 없는 파일(.nojekyll 등)은 글 검사를 하지 않는다
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: UTF-8 글이 아님")
            continue
        problems += [str(f) for f in scan_text(text, rel, extra_deny)]
        # JS에는 아래 주소 검사를 하지 않는다. JS 안의 'http://www.w3.org/2000/svg'는 SVG 이름 공간 상수
        # (createElementNS의 식별자)일 뿐 브라우저가 요청하는 주소가 아니어서, 문자열 검사로는 거짓 양성이 된다.
        # JS의 주소 규칙(상대 경로·fetch·import)은 explorer/tests/test_app.py가 따로 시험한다.
        if ext in (".html", ".htm", ".svg"):
            parser = _LinkParser()
            parser.feed(text)
            parser.close()
            problems += [f"{rel}: {why} (줄 {line})" for line, why in parser.flags]
            for line, attr, value in parser.links:
                kind = _url_problem(value)
                if kind == "relative":
                    kind = _resolve(rel, value, files)
                if kind:
                    problems.append(f"{rel}: {attr} {kind} (줄 {line})")
        if ext == ".css":
            for rx, why in ((_CSS_IMPORT, "CSS @import"), (_CSS_EXT_URL, "CSS 외부 url()")):
                for m in rx.finditer(text):
                    problems.append(f"{rel}: {why} (줄 {text.count(chr(10), 0, m.start()) + 1})")
    return problems
