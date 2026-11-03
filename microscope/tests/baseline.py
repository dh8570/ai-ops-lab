"""비트 일치 회귀 기준(PM 결정). 기준 환경(baseline_env.json)과 같은 환경에서는 비트 불일치를 실패로,
다른 환경에서는 skip(사유에 숫자 포함)으로 처리한다."""
import json
from pathlib import Path

import pytest
import torch
import transformers

BASELINE = json.loads((Path(__file__).parent / "baseline_env.json").read_text())


def current_env() -> dict:
    return {"torch": torch.__version__, "transformers": transformers.__version__,
            "cpu_capability": torch.backends.cpu.get_cpu_capability(), "threads": torch.get_num_threads()}


def env_diff() -> dict:
    cur = current_env()
    return {k: {"baseline": BASELINE[k], "current": v} for k, v in cur.items() if BASELINE[k] != v}


def require_bit_equal(ok: bool, detail: str) -> None:
    """ok가 거짓이면: 기준 환경과 같을 때 실패, 다를 때 skip."""
    if ok:
        return
    diff = env_diff()
    if diff:
        pytest.skip(f"비트 불일치이지만 기준 환경과 다름 {diff}: {detail}")
    pytest.fail(f"비트 불일치(기준 환경과 같음): {detail}")
