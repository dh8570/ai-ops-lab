import sys
from pathlib import Path

# watcher/ 모듈을 패키지 설치 없이 import 하기 위해 루트를 path에 넣는다.
WATCHER_ROOT = Path(__file__).resolve().parent.parent
if str(WATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(WATCHER_ROOT))
