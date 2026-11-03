#!/usr/bin/env bash
# microscope/ 정적 검사와 시험을 실행한다.
#   scripts/check-microscope.sh          ruff check + 모델 없는 시험(V6 연산, V7 저장 형식, V11 뽑기). 수 초
#   scripts/check-microscope.sh --model  위에 더해 모델 시험(V0 토큰화, V1 층 float32, V2~V4 Prefill HF 비교, V5 재현성,
#                                        V8~V10 Decode HF 비교, V12 결정성, V13 첫 토큰). 수 분
#   scripts/check-microscope.sh fix      ruff 자동 수정
# 주의: 이 스크립트는 레포 밖 경로를 쓴다.
#   - 가상환경: $MICROSCOPE_VENV (기본 $HOME/microscope-spike/venv). torch·transformers·pytest·ruff가 여기 있다.
#   - 모델: $MICROSCOPE_MODEL_DIR (기본 $HOME/microscope-spike/model). 모델 시험만 쓴다(읽기 전용).
# 다른 check-*.sh와 달리 컨테이너를 쓰지 않는다(15GB 모델과 CPU용 torch가 호스트 가상환경에 있기 때문).
# 레포에 캐시·바이트코드가 남지 않도록 막는다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${MICROSCOPE_VENV:-$HOME/microscope-spike/venv}"
PY="$VENV/bin/python"
export PYTHONDONTWRITEBYTECODE=1           # __pycache__를 만들지 않는다

cd "$ROOT"
if [[ "${1:-}" == "fix" ]]; then
  "$PY" -m ruff check --no-cache --fix --config microscope/pyproject.toml microscope
  exit 0
fi

"$PY" -m ruff check --no-cache --config microscope/pyproject.toml microscope

if [[ "${1:-}" == "--model" ]]; then
  # -s: 모델 시험이 출력하는 층별 표를 보이게 한다
  "$PY" -m pytest -c microscope/pyproject.toml -p no:cacheprovider -q -s microscope/tests
else
  "$PY" -m pytest -c microscope/pyproject.toml -p no:cacheprovider -q -m "not model" microscope/tests
fi
