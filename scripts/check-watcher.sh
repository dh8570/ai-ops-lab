#!/usr/bin/env bash
# watcher/ 정적 검사와 단위 테스트, scripts/acceptance/ 정적 검사를 일회용 컨테이너에서 실행한다.
#   scripts/check-watcher.sh       ruff check(watcher + acceptance) + pytest(watcher만)
#   scripts/check-watcher.sh fix   ruff 자동 수정(watcher + acceptance)
# acceptance도 watcher의 pyproject.toml 규칙으로 검사한다. 컨테이너가 root로 돌기 때문에
# 레포에 캐시·바이트코드가 남지 않도록 막는다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "${1:-}" == "fix" ]]; then
  CMD="pip install -q --disable-pip-version-check -r requirements-dev.txt && ruff check --no-cache --fix . && ruff check --no-cache --fix --config pyproject.toml /acceptance"
else
  CMD="pip install -q --disable-pip-version-check -r requirements-dev.txt && ruff check --no-cache . && ruff check --no-cache --config pyproject.toml /acceptance && pytest -q -p no:cacheprovider"
fi

docker run --rm \
  -v "$ROOT/watcher:/watcher" -w /watcher \
  -v "$ROOT/scripts/acceptance:/acceptance" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PIP_ROOT_USER_ACTION=ignore \
  python:3.12-slim \
  sh -c "$CMD"
