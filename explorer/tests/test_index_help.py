"""화면 도움말: 지정된 문구가 한 글자도 다르지 않게 들어 있고, 설정마다 ⓘ 버튼이 있다."""
import re

import pytest
from expected_help import CARD_HELP, OPT_HELP

import app as appmod


@pytest.fixture(scope="module")
def html():
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        return c.get("/").get_data(as_text=True)


@pytest.mark.parametrize("key", CARD_HELP)
def test_card_help_text_is_verbatim(html, key):
    assert CARD_HELP[key] in html
    assert f'<p data-help="{key}"></p>' in html          # 본문 자리는 비워 두고 JS가 textContent로 채운다


@pytest.mark.parametrize("key", OPT_HELP)
def test_option_help_text_is_verbatim_and_has_info_button(html, key):
    assert OPT_HELP[key] in html
    assert f'data-opt="{key}"' in html
    # 클릭으로 여는 <button>(키보드 접근 가능), 열림 상태와 연결 대상이 표시된다
    assert re.search(rf'<button type="button" class="info" aria-expanded="false" aria-controls="help-{key}"', html)
    assert f'<div class="opt-help" id="help-{key}" hidden></div>' in html


def test_help_is_set_with_textcontent_not_innerhtml(html):
    js = html[html.index("function initHelp()"):html.index("async function init()")]
    assert "textContent" in js and "innerHTML" not in js and "mouseenter" not in js and "mouseover" not in js
    assert "innerHTML" not in html                       # 페이지 어디에서도 innerHTML을 쓰지 않는다
