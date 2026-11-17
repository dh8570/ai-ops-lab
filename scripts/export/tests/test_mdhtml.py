"""T8b: 안전한 마크다운 → HTML 변환(mdhtml.py) 시험."""
import re

import pytest

from mdhtml import render_fragment, render_page


def test_basic_blocks():
    out = render_fragment("# 제목\n\n문단 **굵게** `코드`\n\n- 하나\n* 둘\n\n1. 첫째\n2. 둘째\n\n---")
    assert "<h1>제목</h1>" in out
    assert "<p>문단 <strong>굵게</strong> <code>코드</code></p>" in out
    assert "<ul>\n<li>하나</li>\n<li>둘</li>\n</ul>" in out
    assert "<ol>\n<li>첫째</li>\n<li>둘째</li>\n</ol>" in out and "<hr>" in out


def test_heading_levels_and_trailing_hashes():
    assert render_fragment("###### 여섯") == "<h6>여섯</h6>"
    assert render_fragment("## C# 언어") == "<h2>C# 언어</h2>"
    assert render_fragment("## 끝 ##") == "<h2>끝</h2>"


@pytest.mark.parametrize("raw", ["<script>alert(1)</script>", '<img src=x onerror="alert(1)">',
                                 "<iframe src=//e.com></iframe>", "<a href=\"javascript:x\">a</a>"])
def test_raw_html_is_shown_as_text(raw):
    out = render_fragment(raw)
    assert "<script" not in out and "<img" not in out and "<iframe" not in out and "<a " not in out
    assert "&lt;" in out


@pytest.mark.parametrize("url", ["javascript:alert(1)", "JAVASCRIPT:x", "https://example.com", "http://e.com/a.html",
                                 "data:text/html,x", "mailto:a", "//e.com/x.html", "/abs.html", "vbscript:x.html",
                                 "javascript:x.html"])
def test_unsafe_links_are_text(url):
    out = render_fragment(f"[보기]({url})")
    assert "href" not in out
    assert out.startswith("<p>보기 (")


@pytest.mark.parametrize("url", ["./a.html", "../b/c.html", "page.html", "page.html#x", "#anchor", "./x.md"])
def test_safe_links(url):
    assert f'<a href="{url}">보기</a>' in render_fragment(f"[보기]({url})")


def test_link_url_cannot_break_attribute_or_get_bold():
    out = render_fragment('[a](./x"onmouseover=1.html) [b](./**y**.html) **c**')
    assert '"onmouseover' not in out and "&quot;onmouseover" in out
    assert '<a href="./**y**.html">b</a>' in out and "<strong>c</strong>" in out


def test_images():
    assert '<img src="./fig.png" alt="그림">' in render_fragment("![그림](./fig.png)")
    assert '<img src="figs/a.PNG" alt="b">' in render_fragment("![b](figs/a.PNG)")
    bad_urls = ("https://e.com/a.png", "//e.com/a.png", "data:image/png;base64,xx", "./a.svg", "/a.png",
                "javascript:a.png")
    for bad in bad_urls:
        out = render_fragment(f"![대체 글]({bad})")
        assert "<img" not in out and out == "<p>대체 글</p>", bad


def test_table_and_escaped_pipe():
    out = render_fragment("| 열 | 값 |\n| --- | :-: |\n| a \\| b | `x` |\n| 짧음 |")
    assert "<th>열</th><th>값</th>" in out
    assert "<td>a | b</td><td><code>x</code></td>" in out
    assert "<tr><td>짧음</td><td></td></tr>" in out


def test_code_block_is_not_interpreted():
    out = render_fragment("```bash\n# 제목 아님\n**굵게 아님** [x](./a.html) <b>\n```\n뒤")
    assert out.startswith("<pre><code># 제목 아님\n**굵게 아님** [x](./a.html) &lt;b&gt;</code></pre>")
    assert "<h1>" not in out and "<strong>" not in out and "href" not in out and "<p>뒤</p>" in out


def test_inline_code_is_not_interpreted():
    assert render_fragment("`**x** <b>`") == "<p><code>**x** &lt;b&gt;</code></p>"


def test_control_chars_cannot_forge_placeholders():
    out = render_fragment("가\x000\x00나 \x01 | x")
    assert "\x00" not in out and "\x01" not in out


def test_page_shell_and_title_escape():
    page = render_page('<제목 & "따옴">', "본문", "./css/site.css")
    assert page.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page and '<meta name="viewport"' in page
    assert "<title>&lt;제목 &amp; &quot;따옴&quot;&gt;</title>" in page
    assert '<link rel="stylesheet" href="./css/site.css">' in page
    assert "<script" not in page and "style=" not in page and "<style" not in page
    assert not re.search(r"https?://|//[a-z]", page)


@pytest.mark.parametrize("css", ["https://e.com/a.css", "//e.com/a.css", "/a.css", "javascript:x"])
def test_page_rejects_external_css(css):
    with pytest.raises(ValueError):
        render_page("t", "x", css)


def test_deterministic():
    md = "# t\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x](./a.html)\n\n```\ncode\n```\n"
    assert render_page("t", md, "./s.css") == render_page("t", md, "./s.css")
