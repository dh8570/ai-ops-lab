"""채팅 템플릿으로 프롬프트 글을 만들고 토큰화한다(transformers 토크나이저 사용, add_special_tokens=False)."""
from dataclasses import dataclass
from functools import lru_cache

from transformers import AutoTokenizer

from .config import model_dir

DEFAULT_USER_TEXT = "웹 서비스에서 1분 동안 요청의 37%가 HTTP 500 에러였다. 원인 후보 두 가지를 한국어로 한 줄씩 써라."


def _bytes_to_unicode() -> dict[int, str]:
    """ByteLevel BPE의 바이트 → 글자 표(GPT-2와 같음). 토큰 조각을 원래 바이트로 되돌리는 데 쓴다."""
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs, n = bs[:], 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs, strict=True)}


BYTE_DECODER = {v: k for k, v in _bytes_to_unicode().items()}


@lru_cache(maxsize=2)
def tokenizer(path: str | None = None):
    return AutoTokenizer.from_pretrained(path or str(model_dir()))


@dataclass
class Prompt:
    user_text: str
    template_string: str
    token_ids: list[int]
    n_prefix: int        # 사용자 글 앞 틀의 토큰 수
    n_suffix: int        # 사용자 글 뒤 틀의 토큰 수


def encode(tok, text: str) -> list[int]:
    return tok(text, add_special_tokens=False)["input_ids"]


def build_prompt(user_text: str = DEFAULT_USER_TEXT, tok=None) -> Prompt:
    tok = tok or tokenizer()
    s = tok.apply_chat_template([{"role": "user", "content": user_text}], add_generation_prompt=True, tokenize=False)
    ids = encode(tok, s)
    if s.count(user_text) != 1:
        raise ValueError("템플릿 문자열 안에서 사용자 글 위치를 하나로 정할 수 없다")
    a = s.index(user_text)
    pre, mid, suf = encode(tok, s[:a]), encode(tok, user_text), encode(tok, s[a + len(user_text):])
    if pre + mid + suf != ids:
        raise ValueError("앞 틀·사용자 글·뒤 틀을 따로 토큰화한 결과가 전체 토큰화와 다르다(경계에서 조각이 합쳐짐)")
    return Prompt(user_text, s, ids, len(pre), len(suf))


def token_bytes(tok, i: int) -> bytes:
    piece = tok.convert_ids_to_tokens(int(i))
    if all(c in BYTE_DECODER for c in piece):
        return bytes(BYTE_DECODER[c] for c in piece)
    return piece.encode()          # 특수 토큰처럼 표에 없는 글자가 있으면 글자 그대로


def token_table(tok, ids: list[int]) -> list[dict]:
    """manifest.prompt.tokens: index, id, piece(조각 하나를 글로 풀어 쓴 것), hex(원래 바이트)."""
    return [{"index": n, "id": int(i), "piece": tok.decode([int(i)]), "hex": token_bytes(tok, i).hex()}
            for n, i in enumerate(ids)]
