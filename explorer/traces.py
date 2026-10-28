"""요청 기록(traces.jsonl). 한 요청에 한 줄. 나중에 Postgres로 옮긴다."""
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()


def append_trace(path: str, record: dict) -> None:
    """기록 실패가 요청을 망치지 않게 오류는 표준 오류로만 남긴다."""
    line = json.dumps({"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **record}, ensure_ascii=False)
    try:
        with _lock:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"trace 기록 실패: {e}", file=sys.stderr)
