"""V0 토큰화: 우리 chat.py의 토큰 번호 = HF apply_chat_template(tokenize=True)의 번호.
진짜 tokenizer 파일(모델 폴더)을 쓰므로 model 마커를 단다."""
import pytest

from microscope.engine import chat

pytestmark = pytest.mark.model

CASES = {
    "default": chat.DEFAULT_USER_TEXT,
    "english": "The web service returned HTTP 500 for 37% of requests in one minute; list two likely causes.",
    "emoji_newline": "서버가 갑자기 느려졌다 🔥\n로그를 보고 원인을 알려 줘 👀🙏",
}


@pytest.mark.parametrize("name", list(CASES))
def test_ids_match_hf(name):
    tok = chat.tokenizer()
    text = CASES[name]
    p = chat.build_prompt(text, tok)
    ref = tok.apply_chat_template([{"role": "user", "content": text}], add_generation_prompt=True, tokenize=True)
    ref_ids = ref["input_ids"] if hasattr(ref, "keys") else ref        # v5는 BatchEncoding을 돌려준다
    print(f"V0 {name}: 토큰 {len(p.token_ids)} (앞 틀 {p.n_prefix}, 뒤 틀 {p.n_suffix})")
    assert p.token_ids == list(ref_ids)


def test_default_counts():
    p = chat.build_prompt()
    print(f"V0 기본 입력: 전체 {len(p.token_ids)}, 앞 틀 {p.n_prefix}, 뒤 틀 {p.n_suffix}, "
          f"사용자 글 {len(p.token_ids) - p.n_prefix - p.n_suffix}")
    assert len(p.token_ids) > p.n_prefix + p.n_suffix
