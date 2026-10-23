#!/usr/bin/env python3
"""설계 그림 5장(fig1~fig5)을 한 번에 다시 만든다.

사용법 (레포 루트에서):
    python3 docs/figures/src/build.py

- 출력: docs/figures/figN_*.png (3840x2160, 256색 압축, 장당 약 150KB)
- 버전 표기는 lib.py의 VERSION 한 곳에서 바꾼다.
- 그림 내용을 바꿀 때는 해당 figN.py의 텍스트·좌표를 고치고 이 스크립트를 다시 실행한다.
"""
import os
import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = SRC.parent                                  # docs/figures/
os.environ["FIG_OUT"] = str(OUT)
sys.path.insert(0, str(SRC))                      # `from lib import *`가 동작하도록

print(f"그림 생성: {OUT}")
for n in range(1, 6):
    runpy.run_path(str(SRC / f"fig{n}.py"))
print("완료")
