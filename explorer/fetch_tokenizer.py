"""tokenizer.json을 고정된 revision으로 한 번 내려받아 sha256을 확인하고 저장한다.

사용: python fetch_tokenizer.py [저장 경로]   (기본: /opt/tokenizer/tokenizer.json)
Docker 빌드 중에만 실행한다. 서버가 돌 때는 인터넷을 쓰지 않는다.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

import tokenizer_pin as pin


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/tokenizer/tokenizer.json")
    with urllib.request.urlopen(pin.URL, timeout=60) as resp:  # noqa: S310 (고정된 https 주소)
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != pin.SHA256:
        print(f"sha256 불일치: 기대 {pin.SHA256}, 실제 {digest}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"저장: {out} ({len(data)} bytes, sha256 {digest}, revision {pin.REVISION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
