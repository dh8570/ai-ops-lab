"""V8~V10 Decode(bf16, 탐욕 8토큰): 우리 엔진(logits_path="bf16", temperature 0)으로 임시 캡처를 만들고
엔진을 해제한 뒤 HF eager와 비교한다(verify.verify_run).
V8: 토큰열 = HF 반복 탐욕 = HF generate(do_sample=False, repetition_penalty=1.0).
V9·V10: 단계별 값·마지막 KV 캐시·어텐션이 비트 단위로 같다(기준 환경이 다르면 skip)."""
import gc

import pytest

from microscope import run, verify, verify_decode
from microscope.engine import chat
from microscope.engine.sampler import SamplingConfig
from microscope.tests.baseline import require_bit_equal

pytestmark = pytest.mark.model
MAX_NEW = 8


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    run_dir = run.run(chat.DEFAULT_USER_TEXT, tmp_path_factory.mktemp("runs"), SamplingConfig(), MAX_NEW, "bf16")
    gc.collect()                                   # run() 안에서 올린 우리 엔진은 여기서 이미 내려가 있다
    res = verify.verify_run(run_dir)
    verify_decode.print_tables(res["decode"])
    yield res["decode"]
    gc.collect()


def test_v8_token_count(result):
    assert len(result["ours_tokens"]) == MAX_NEW and result["n_decode_steps"] == MAX_NEW - 1


def test_v8a_same_as_hf_loop(result):
    assert result["v8a_same"], (result["ours_tokens"], result["hf_greedy_tokens"])


def test_v8b_same_as_hf_generate(result):
    assert result["v8b_scores_equal_raw_logits"], "generate의 처리기가 로짓을 바꿨다(repetition_penalty 덮어쓰기 실패?)"
    assert result["v8b_same"], (result["ours_tokens"], result["v8b_tokens"])


def test_v9_steps_bit_equal(result):
    keys = ("resid2", "k", "v", "final_norm", "logits_bf16")
    bad = [(s["step"], {k: s[k]["max_abs"] for k in keys}) for s in result["v9_steps"]
           if not all(s[k]["bit_equal"] for k in keys)]
    require_bit_equal(not bad, f"V9 단계(최대 절대 차이) {bad}")


def test_v9_final_cache_bit_equal(result):
    bad = [(c["layer"], c["k"]["max_abs"], c["v"]["max_abs"]) for c in result["v9_cache"]
           if not (c["k"]["bit_equal"] and c["v"]["bit_equal"])]
    require_bit_equal(not bad, f"V9 마지막 캐시 층(K 최대, V 최대) {bad}")


def test_v10_attention_bit_equal(result):
    bad = [(r["step"], max(r["by_layer"])) for r in result["v10_attn"] if not r["bit_equal"]]
    require_bit_equal(not bad, f"V10 단계(층 중 최대 절대 차이) {bad}")


def test_info_f32_vs_bf16_greedy(result):
    """정보(실패 아님): float32 로짓의 탐욕이 bf16 로짓의 탐욕과 다른 단계."""
    print("V8 정보: f32·bf16 탐욕이 다른 단계", result["info_f32_vs_bf16_greedy_differs"])
