"""Ollama /api/generate 스트림(NDJSON)을 조각 단위 사건으로 바꾼다. 시계를 바깥에서 받아 테스트할 수 있다."""
import json

# 마지막 줄에서 그대로 보여 줄 통계. 'context'(토큰 id 배열)는 커서 뺀다.
STAT_KEYS = ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_cached_count",
             "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def parse_stream(lines, clock, t0: float):
    """lines: 줄(bytes 또는 str)의 반복자. clock: 단조 시계(초). t0: 요청을 보내기 시작한 시각.

    chunk 사건의 gap_ms는 직전 조각(첫 조각은 t0)과의 간격, since_start_ms는 t0 이후 누적이다.
    마지막(done) 줄의 통계와, 받은 조각을 이어 붙인 글(joined), 마지막 줄의 response 필드를 그대로 싣는다.
    """
    count, prev, parts = 0, t0, []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = raw.strip()
        if not raw:
            continue
        now = clock()                                  # 줄을 받은 시각 = 서버가 조각을 받은 시각
        try:
            obj = json.loads(raw)
        except ValueError:
            yield {"type": "error", "message": "JSON이 아닌 줄을 받았습니다.", "raw": raw[:200]}
            return
        if not isinstance(obj, dict):
            yield {"type": "error", "message": "객체가 아닌 JSON 줄을 받았습니다.", "raw": raw[:200]}
            return
        if "error" in obj:
            yield {"type": "error", "message": str(obj["error"])}
            return
        text = obj.get("response", "")
        if not isinstance(text, str):
            text = ""
        if not obj.get("done") or text:                # done 줄에 글이 실려 오면 그것도 조각으로 센다
            count += 1
            parts.append(text)
            yield {"type": "chunk", "n": count, "text": text,
                   "gap_ms": _ms(now - prev), "since_start_ms": _ms(now - t0)}
            prev = now
        if obj.get("done"):
            yield {"type": "done",
                   "stats": {k: obj[k] for k in STAT_KEYS if k in obj},
                   "chunks": count,
                   "joined": "".join(parts),
                   "final_response": obj.get("response"),
                   "since_start_ms": _ms(now - t0)}
            return
    yield {"type": "error", "message": "done 줄 없이 스트림이 끝났습니다.", "chunks": count, "joined": "".join(parts)}
