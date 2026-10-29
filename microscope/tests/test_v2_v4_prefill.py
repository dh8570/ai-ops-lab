"""V2~V4 전체 Prefill(bf16): 우리 엔진으로 임시 캡처를 만들고 엔진을 해제한 뒤 HF eager와 비교한다.
회귀 기준은 비트 일치(PM 결정): 층 출력·K·V·어텐션이 HF eager와 torch.equal이어야 한다.
기준 환경(baseline_env.json)과 다르면 비트 불일치는 skip(사유에 숫자 포함)."""
import gc

import pytest
import torch

from microscope import run, verify
from microscope.engine import capture, chat, config, model, weights
from microscope.tests.baseline import require_bit_equal

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


def test_tables_cover_all_layers(result):
    n = config.load_config().layers
    assert len(result["v3_kv"]) == n and len(result["v4_attn"]) == n and len(result["v2_layers"]) == n + 2


def test_v2_layer_outputs_bit_equal(result):
    bad = [(r["layer"], r["max_abs"]) for r in result["v2_layers"] if not r["bit_equal"]]
    require_bit_equal(not bad, f"V2 층(최대 절대 차이) {bad}")


def test_v3_kv_bit_equal(result):
    bad = [(r["layer"], r["k"]["max_abs"], r["v"]["max_abs"]) for r in result["v3_kv"]
           if not (r["k"]["bit_equal"] and r["v"]["bit_equal"])]
    require_bit_equal(not bad, f"V3 층(K 최대, V 최대) {bad}")


def test_v4_attention_bit_equal(result):
    bad = [(r["layer"], r["max_abs"]) for r in result["v4_attn"] if not r["bit_equal"]]
    require_bit_equal(not bad, f"V4 층(최대 절대 차이) {bad}")
