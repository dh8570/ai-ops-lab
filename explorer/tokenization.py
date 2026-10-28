"""토큰화: tokenizers가 준 토큰을 바이트 단위로 풀어서 보여 준다.

Qwen2.5의 tokenizer.json은 ByteLevel(GPT-2식 바이트 매핑)이다. 토큰 문자열의 글자 하나는 바이트 하나를
가리키는 대리 글자이므로, 역매핑으로 되돌리면 토큰이 실제로 담은 바이트를 얻는다.
"""
import unicodedata
from bisect import bisect_right


def bytes_to_unicode() -> dict[int, str]:
    """GPT-2의 bytes_to_unicode: 바이트 0~255를 눈에 보이는 유니코드 글자 256개에 일대일로 대응시킨다."""
    printable = (list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1))
                 + list(range(ord("®"), ord("ÿ") + 1)))
    byte_values, chars = printable[:], printable[:]
    extra = 0
    for b in range(256):
        if b not in printable:                  # 공백·제어문자 등은 256 이상의 글자로 옮긴다
            byte_values.append(b)
            chars.append(256 + extra)
            extra += 1
    return {b: chr(c) for b, c in zip(byte_values, chars, strict=True)}


BYTE_DECODER = {ch: b for b, ch in bytes_to_unicode().items()}


def token_to_bytes(token: str) -> bytes:
    """ByteLevel 토큰 문자열 -> 원래 바이트. 매핑에 없는 글자가 있으면 ValueError."""
    try:
        return bytes(BYTE_DECODER[ch] for ch in token)
    except KeyError as e:
        raise ValueError(f"ByteLevel 매핑에 없는 글자: {e.args[0]!r} (토큰 {token!r})") from None


def is_valid_utf8(data: bytes) -> bool:
    """이 바이트 조각만으로 올바른 UTF-8 글자열인가. 잘린 한글 조각이면 False."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def char_spans(text: str) -> list[tuple[str, int, int]]:
    """각 글자(코드포인트)가 UTF-8 바이트열의 어디부터 어디까지인지: (글자, 시작, 끝)."""
    spans, pos = [], 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        spans.append((ch, pos, pos + n))
        pos += n
    return spans


def count_tokens(tk, text: str) -> int:
    return len(tk.encode(text, add_special_tokens=False).ids)


def tokenize_text(tk, text: str) -> dict:
    """text를 순수 입력 글자만(add_special_tokens=False) 잘라 토큰별 실측 정보를 돌려준다."""
    # tokenizer.json의 normalizer가 NFC라서, 토큰 바이트는 입력이 아니라 NFC로 바꾼 입력과 같아진다.
    normalized = unicodedata.normalize("NFC", text)
    enc = tk.encode(text, add_special_tokens=False)
    special_ids = set(tk.get_added_tokens_decoder())
    spans = char_spans(normalized)
    starts = [s for _, s, _ in spans]

    rows, joined, offset = [], bytearray(), 0
    for index, (tid, tok) in enumerate(zip(enc.ids, enc.tokens, strict=True)):
        special = tid in special_ids
        raw = tok.encode("utf-8") if special else token_to_bytes(tok)   # 특수 토큰은 이름 그대로가 내용
        start, end = offset, offset + len(raw)
        offset = end
        joined += raw
        valid = is_valid_utf8(raw)
        cut = []                                  # 이 토큰이 중간을 자르고 지나가는 글자들
        j = max(bisect_right(starts, start) - 1, 0)
        while j < len(spans) and spans[j][1] < end:
            ch, s, e = spans[j]
            if s < start or e > end:
                cut.append(ch)
            j += 1
        rows.append({
            "index": index,
            "id": tid,
            "piece": raw.decode("utf-8", errors="replace"),
            "hex": raw.hex(" "),
            "valid_utf8": valid,
            "partial": not valid,                  # 깨진 글자(바이트 조각)
            "special": special,
            "fragment_of": cut,
        })
    return {
        "text_chars": len(text),
        "text_bytes": len(text.encode("utf-8")),
        "nfc_changed": normalized != text,
        "normalized_bytes": len(normalized.encode("utf-8")),
        "token_count": len(rows),
        "partial_count": sum(1 for r in rows if r["partial"]),
        "bytes_match": bytes(joined) == normalized.encode("utf-8"),
        "tokens": rows,
    }
