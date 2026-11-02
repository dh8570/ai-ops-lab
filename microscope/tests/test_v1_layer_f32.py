"""V1 층 하나(float32): 층 0, 중간, 마지막에서 우리 층과 HF Qwen2DecoderLayer를 같은 입력으로 비교.
기준 최대 절대 차이 ≤ 1e-4.
넘으면 처음 벌어지는 하위 단계를 실패 메시지에 담는다(기준은 풀지 않는다)."""
import gc

import pytest

from microscope import verify_layer
from microscope.engine import config

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def result():
    n = config.load_config().layers
    res = verify_layer.verify_layers([0, n // 2, n - 1])
    verify_layer.print_table(res)
    yield res
    gc.collect()


def test_cos_sin_and_mask_match_hf(result):
    assert result["cos_sin_equal_hf"] and result["mask_equal_hf"]


@pytest.mark.parametrize("pos", [0, 1, 2])
def test_layer_output_within_tolerance(result, pos):
    r = result["layers"][pos]
    assert r["output"]["max_abs"] <= verify_layer.TOL, (
        f"층 {r['layer']}: 최대 절대 차이 {r['output']['max_abs']}, 처음 넘는 단계 {r['first_stage_over_tol']}")
