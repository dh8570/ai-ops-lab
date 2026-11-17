"""안전한 마크다운 → HTML 변환 (T8b). 표준 라이브러리만 쓴다.

지원: 제목(#~######), 문단, 목록(-, *, 1.), 표(|와 구분줄), 코드 블록(```), 인라인 코드(`), 굵게(**),
링크 [글](주소), 이미지 ![alt](경로), 가로줄(---).
보안 규칙
- 모든 글을 먼저 html.escape한 뒤에 인라인 표시를 처리한다. 원시 HTML 태그는 글자로 보인다.
- 링크는 상대 경로(./ 또는 ../로 시작, 또는 /가 없는 .html 파일)와 #앵커만 href가 된다. 그 밖(http, https,
  javascript:, data:, mailto:, //, 스킴이 있는 모든 주소)은 "글 (주소)" 글자로만 보인다.
- 이미지는 상대 경로 .png만 img가 되고, 그 밖은 alt 글자만 보인다.
- 만든 HTML 조각은 자리표시로 숨겨 두었다가 마지막에 되돌린다. 그래서 굵게(**) 처리가 href 안으로 들어가지 않는다.
- 페이지에는 스크립트·인라인 스타일·외부 주소를 넣지 않는다. 같은 입력이면 같은 출력이다.
"""
from __future__ import annotations

import html
import re

_PH = re.compile("\x00(\\d+)\x00")                       # 자리표시: \x00번호\x00 (입력의 \x00은 미리 지운다)
_CODE = re.compile(r"`([^`]+)`")
_URL = r"((?:[^()\s]|\([^()\s]*\))*)"                # 주소 안의 괄호는 한 겹까지 (예: alert(1), a(1).html)
_IMG = re.compile(r"!\[([^\]]*)\]\(" + _URL + r"\)")
_LINK = re.compile(r"\[([^\]]+)\]\(" + _URL + r"\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HEAD = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_UL = re.compile(r"^\s*[-*]\s+(.*)$")
_OL = re.compile(r"^\s*\d+\.\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,})\s*$")
_TSEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*```")
_PIPE = "\x01"                                           # 표 셀 안의 \| 자리 (입력의 \x01도 미리 지운다)


def _plain_relative(url: str) -> bool:
    """스킴·//·/·역슬래시·제어 글자가 없는 상대 경로인가."""
    if url == "" or "\\" in url or any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return False
    return not (url.startswith("/") or _SCHEME.match(url))


def _safe_url(url: str) -> str | None:
    """링크로 만들어도 되는 주소의 종류. "anchor" / "relative" / None(링크로 만들지 않음)."""
    if url.startswith("#") and _plain_relative(url[1:] or "x"):
        return "anchor"
    if not _plain_relative(url):
        return None
    if url.startswith(("./", "../")):
        return "relative"
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    if "/" not in path and path.endswith(".html"):
        return "relative"
    return None


def _inline(text: str) -> str:
    """한 줄(또는 셀) 글을 escape한 뒤 인라인 표시를 처리한다."""
    store: list[str] = []

    def keep(fragment: str) -> str:
        store.append(fragment)
        return f"\x00{len(store) - 1}\x00"

    s = html.escape(text, quote=True)                    # 먼저 전부 escape
    s = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), s)

    def img(m):
        alt, url_esc = m.group(1), m.group(2)
        url = html.unescape(url_esc)
        path = re.split(r"[?#]", url, maxsplit=1)[0]
        if _plain_relative(url) and path.lower().endswith(".png"):
            return keep(f'<img src="{url_esc}" alt="{alt}">')
        return keep(alt)

    def link(m):
        label, url_esc = _BOLD.sub(r"<strong>\1</strong>", m.group(1)), m.group(2)
        if _safe_url(html.unescape(url_esc)):
            return keep(f'<a href="{url_esc}">{label}</a>')
        return keep(f"{label} ({url_esc})")

    s = _IMG.sub(img, s)
    s = _LINK.sub(link, s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    while _PH.search(s):                                 # 조각 안에 조각이 있을 수 있어 없어질 때까지 되돌린다
        s = _PH.sub(lambda m: store[int(m.group(1))], s)
    return s


def _cells(line: str) -> list[str]:
    s = line.strip().replace("\\|", _PIPE)
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace(_PIPE, "|") for c in s.split("|")]


def _table(rows: list[str]) -> str:
    head, body = _cells(rows[0]), [_cells(r) for r in rows[2:]]
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>"]
    if body:
        out.append("<tbody>")
        for r in body:
            r = (r + [""] * len(head))[:len(head)]       # 칸 수를 머리줄에 맞춘다
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "\n".join(out)


def _is_block_start(lines: list[str], i: int) -> bool:
    ln = lines[i]
    return bool(_FENCE.match(ln) or _HEAD.match(ln) or _UL.match(ln) or _OL.match(ln) or _HR.match(ln)
                or (ln.lstrip().startswith("|") and i + 1 < len(lines) and _TSEP.match(lines[i + 1])))


def render_fragment(md_text: str) -> str:
    """마크다운 글을 HTML 조각으로 바꾼다."""
    text = md_text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd").replace(_PIPE, "\ufffd")
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "":
            i += 1
        elif _FENCE.match(ln):
            i += 1
            code = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1                                       # 닫는 ``` (없으면 문서 끝까지가 코드)
            out.append("<pre><code>" + html.escape("\n".join(code), quote=True) + "</code></pre>")
        elif m := _HEAD.match(ln):
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
        elif _HR.match(ln):
            out.append("<hr>")
            i += 1
        elif ln.lstrip().startswith("|") and i + 1 < len(lines) and _TSEP.match(lines[i + 1]):
            rows = [ln, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
        elif _UL.match(ln) or _OL.match(ln):
            rx, tag = (_UL, "ul") if _UL.match(ln) else (_OL, "ol")
            items = []
            while i < len(lines) and (m := rx.match(lines[i])):
                items.append(f"<li>{_inline(m.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() != "" and (not para or not _is_block_start(lines, i)):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>" + "\n".join(_inline(p) for p in para) + "</p>")
    return "\n".join(out)


def render_page(title: str, md_text: str, css_href: str) -> str:
    """한 페이지 HTML. css_href는 상대 경로여야 한다(외부 주소면 ValueError)."""
    if _safe_url(css_href) != "relative":
        raise ValueError("css_href는 상대 경로여야 함")
    return ("<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{html.escape(title, quote=True)}</title>\n"
            f"<link rel=\"stylesheet\" href=\"{html.escape(css_href, quote=True)}\">\n"
            "</head>\n<body>\n<main>\n" + render_fragment(md_text) + "\n</main>\n</body>\n</html>\n")
