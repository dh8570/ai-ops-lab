"""토큰화: 토큰 바이트를 이으면 입력의 UTF-8 바이트와 같은가, 깨진 조각 판정, ByteLevel 역매핑."""
import pytest

import tokenization as T

SAMPLES = {
    "영어": "Hello, world! HTTP 500 at 12:34:56",
    "한국어": "웹 서비스에서 1분 동안 요청의 37%가 HTTP 500 에러였다. 원인 후보 두 가지를 한국어로 한 줄씩 써라.",
    "이모지": "배포 완료 😀 🫠 👍🏽 끝",
    "줄바꿈": "첫 줄\n\n둘째 줄\r\n\t탭과 끝 공백  ",
    "갈라지는 한글": "꿻 귋 쀍",
}


@pytest.mark.parametrize("text", SAMPLES.values(), ids=SAMPLES.keys())
def test_token_bytes_join_equals_input_bytes(tk, text):
    r = T.tokenize_text(tk, text)
    joined = b"".join(bytes.fromhex(t["hex"]) for t in r["tokens"])
    assert joined == text.encode("utf-8")           # 이 샘플들은 이미 NFC라 정규화 전후가 같다
    assert r["bytes_match"] is True
    assert r["nfc_changed"] is False
    assert r["text_bytes"] == len(text.encode("utf-8"))
    assert r["token_count"] == len(r["tokens"])


def test_korean_char_split_into_byte_fragments(tk):
    r = T.tokenize_text(tk, "꿻")                    # 실제 토크나이저가 'ea bf' + 'bb' 두 조각으로 자름
    assert [t["hex"] for t in r["tokens"]] == ["ea bf", "bb"]
    assert all(t["partial"] and not t["valid_utf8"] for t in r["tokens"])
    assert all(t["fragment_of"] == ["꿻"] for t in r["tokens"])
    assert r["partial_count"] == 2


def test_whole_characters_are_not_partial(tk):
    r = T.tokenize_text(tk, "Hello 😀")
    assert r["partial_count"] == 0
    assert all(t["valid_utf8"] and t["fragment_of"] == [] for t in r["tokens"])


def test_leading_space_can_split_a_hangul_char(tk):
    # 실측: ' 웹'은 '20 ec 9b' + 'b9'로 잘린다. 같은 글자도 앞 공백 유무에 따라 한 토큰이거나 쪼개진다.
    r = T.tokenize_text(tk, "Hello 웹")
    frags = [t for t in r["tokens"] if t["partial"]]
    assert [t["hex"] for t in frags] == ["20 ec 9b", "b9"]
    assert all(t["fragment_of"] == ["웹"] for t in frags)
    assert T.tokenize_text(tk, "웹")["partial_count"] == 0


def test_nfc_normalization_is_reported(tk):
    decomposed = "한"                 # 자모로 풀어 쓴 '한'
    r = T.tokenize_text(tk, decomposed)
    assert r["nfc_changed"] is True
    assert r["bytes_match"] is True                   # 토큰 바이트 = NFC로 바꾼 입력의 바이트
    assert b"".join(bytes.fromhex(t["hex"]) for t in r["tokens"]) == "한".encode()


def test_special_token_text_is_flagged(tk):
    r = T.tokenize_text(tk, "<|im_end|>")
    assert r["tokens"][0]["special"] is True
    assert bytes.fromhex(r["tokens"][0]["hex"]) == b"<|im_end|>"


def test_no_special_tokens_are_added(tk):
    r = T.tokenize_text(tk, "안녕")
    assert not any(t["special"] for t in r["tokens"])   # add_special_tokens=False


def test_count_tokens_matches_tokenize(tk):
    text = SAMPLES["한국어"]
    assert T.count_tokens(tk, text) == T.tokenize_text(tk, text)["token_count"]


# ---- 단독으로 올바른 UTF-8인가 ----
def test_is_valid_utf8_whole_samples():
    assert T.is_valid_utf8("한".encode()) is True
    assert T.is_valid_utf8("😀".encode()) is True
    assert T.is_valid_utf8(b"abc") is True


def test_is_valid_utf8_truncated_hangul():
    han = "한".encode()                               # ed 95 9c
    assert T.is_valid_utf8(han[:1]) is False          # 첫 바이트만
    assert T.is_valid_utf8(han[:2]) is False          # 둘째까지만
    assert T.is_valid_utf8(han[1:]) is False          # 앞이 잘린 이어짐 바이트
    assert T.is_valid_utf8("😀".encode()[:3]) is False
    assert T.is_valid_utf8(b"\xff") is False


# ---- ByteLevel 역매핑 ----
def test_bytes_to_unicode_is_a_bijection_over_256_bytes():
    m = T.bytes_to_unicode()
    assert sorted(m) == list(range(256))
    assert len(set(m.values())) == 256
    assert all(m[b] == chr(b) for b in range(ord("!"), ord("~") + 1))   # 눈에 보이는 ASCII는 그대로


def test_token_to_bytes_known_values():
    assert T.token_to_bytes("Ġ") == b" "              # 공백은 'Ġ'로 표기됨
    assert T.token_to_bytes("Ċ") == b"\n"
    assert T.token_to_bytes("ìķĪ") == "안".encode()


def test_token_to_bytes_rejects_unmapped_char():
    with pytest.raises(ValueError):
        T.token_to_bytes("한")                         # ByteLevel 글자가 아닌 진짜 한글은 매핑에 없음
