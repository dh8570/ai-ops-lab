"""V2~V4 전체 Prefill(bf16): 우리 엔진으로 임시 캡처를 만들고 엔진을 해제한 뒤 HF eager와 비교한다.
단언은 V2의 "1등이 같다"와 "상위 5개 집합이 같다" 둘뿐이다. 나머지(층별 차이, K·V, 어텐션)는 숫자만 출력한다."""
import gc

import pytest
import torch

from microscope import run, verify
from microscope.engine import capture, chat, config, model, weights

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    cfg = config.load_config()
    tok = chat.tokenizer()
    prompt = chat.build_prompt(tok=tok)
    run_dir = tmp_path_factory.mktemp("runs") / capture.new_run_id()
    w = weights.load_all(cfg)
    rec = capture.Recorder("prefill")
    model.prefill(prompt.token_ids, w, cfg, torch.bfloat16, rec)
    del w
    gc.collect()                                   # 우리 엔진을 내린 뒤에 HF를 올린다
    man = run.build_manifest(run_dir.name, cfg, prompt, tok, "bf16")
    rec.write(run_dir, man)
    del rec
    gc.collect()
    res = verify.verify_run(run_dir)
    verify.print_tables(res)
    yield res
    gc.collect()


def test_v2_top1_same(result):
    assert result["v2_top1_same"]


def test_v2_top5_set_same(result):
    assert result["v2_top5_set_same"]


def test_v3_v4_reported(result):
    """숫자만 보고(한도는 PM이 정한다). 표가 모든 층을 담았는지만 확인."""
    n = config.load_config().layers
    assert len(result["v3_kv"]) == n and len(result["v4_attn"]) == n and len(result["v2_layers"]) == n + 2
