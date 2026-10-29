"""V5 재현성: 같은 입력으로 우리 Prefill을 두 번 돌려 캡처한 모든 텐서와 MLP 요약이 비트 단위로 같다."""
import gc

import pytest
import torch

from microscope.engine import capture, chat, config, model, weights

pytestmark = pytest.mark.model


def test_two_runs_bit_identical():
    cfg = config.load_config()
    ids = chat.build_prompt().token_ids
    w = weights.load_all(cfg)
    recs = []
    for _ in range(2):
        rec = capture.Recorder("prefill")
        model.prefill(ids, w, cfg, torch.bfloat16, rec)
        recs.append(rec)
    del w
    gc.collect()
    a, b = recs
    assert list(a.tensors) == list(b.tensors)
    diff = [k for k in a.tensors if not torch.equal(a.tensors[k][1], b.tensors[k][1])]
    print(f"V5: 텐서 {len(a.tensors)}개 중 다른 것 {len(diff)}개 {diff[:5]}")
    assert diff == []
    assert a.mlp_summary == b.mlp_summary
