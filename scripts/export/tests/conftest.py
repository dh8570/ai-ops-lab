"""scripts/export를 import 경로에 둔다. 레포에서도, check-export.sh 컨테이너(/repo/scripts/export)에서도 같다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
