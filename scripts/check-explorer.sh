#!/usr/bin/env bash
# explorer/ 정적 검사와 단위 테스트를 일회용 컨테이너에서 실행한다.
#   scripts/check-explorer.sh       ruff check + pytest
#   scripts/check-explorer.sh fix   ruff 자동 수정(이때만 소스를 쓰기 가능으로 마운트)
# 토크나이저 테스트가 진짜 tokenizer.json을 쓰므로, 빌드와 같은 fetch_tokenizer.py로(revision 고정, sha256 확인) 받는다.
# 컨테이너가 root로 돌기 때문에 레포에 캐시·바이트코드가 남지 않도록 막는다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP="pip install -q --disable-pip-version-check -r requirements.txt -r requirements-dev.txt && python fetch_tokenizer.py /tmp/tokenizer.json"

if [[ "${1:-}" == "fix" ]]; then
  MOUNT="$ROOT/explorer:/explorer"
  CMD="$SETUP && ruff check --no-cache --fix ."
else
  MOUNT="$ROOT/explorer:/explorer:ro"        # 검사만 하므로 읽기 전용
  CMD="$SETUP && ruff check --no-cache . && pytest -q -p no:cacheprovider"
fi

docker run --rm \
  -v "$MOUNT" -w /explorer \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PIP_ROOT_USER_ACTION=ignore \
  -e TOKENIZER_PATH=/tmp/tokenizer.json \
  python:3.12-slim \
  sh -c "$CMD"
