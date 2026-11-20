#!/usr/bin/env bash
# scripts/export/(공개 내보내기 안전 게이트) 정적 검사와 단위 테스트를 일회용 컨테이너에서 실행한다.
#   scripts/check-export.sh       ruff check + pytest
#   scripts/check-export.sh fix   ruff 자동 수정(이때만 scripts/export를 쓰기 가능으로 마운트)
# ruff 규칙은 watcher/pyproject.toml을 그대로 쓴다. export_site.py가 watcher/*.py(llm_trace)와 explorer/*.py(runs, app)를
# 읽고, 시험이 실제 파일(explorer/static, docs/figures)을 읽으므로 레포와 같은 경로 모양(/repo/...)으로
# 읽기 전용 마운트한다. 시험은 Path(__file__).parents[3]으로 레포 루트를 구한다.
# 컨테이너가 root로 돌기 때문에 레포에 캐시·바이트코드가 남지 않도록 막는다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP="pip install -q --disable-pip-version-check -r /repo/watcher/requirements-dev.txt"
RUFF="ruff check --no-cache --config /repo/watcher/pyproject.toml"

if [[ "${1:-}" == "fix" ]]; then
  EXPORT_MOUNT="$ROOT/scripts/export:/repo/scripts/export"
  CMD="$SETUP && $RUFF --fix ."
else
  EXPORT_MOUNT="$ROOT/scripts/export:/repo/scripts/export:ro"   # 검사만 하므로 읽기 전용
  # -c /dev/null: 상위 폴더의 설정 파일을 찾지 않게 한다. --rootdir: 시험 경로 기준을 고정한다
  CMD="$SETUP && $RUFF . && pytest -q -p no:cacheprovider -c /dev/null --rootdir /repo/scripts/export tests"
fi

docker run --rm \
  -v "$EXPORT_MOUNT" -w /repo/scripts/export \
  -v "$ROOT/watcher:/repo/watcher:ro" \
  -v "$ROOT/explorer:/repo/explorer:ro" \
  -v "$ROOT/docs/figures:/repo/docs/figures:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PIP_ROOT_USER_ACTION=ignore \
  python:3.12-slim \
  sh -c "$CMD"
