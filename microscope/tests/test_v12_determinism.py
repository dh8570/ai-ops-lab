"""V12 결정성: 같은 입력·같은 설정으로 Decode까지(새 토큰 8개) 두 번 돌려 모든 캡처 텐서와 토큰이 비트 단위로 같다.
정보: 스레드 6개와 8개로 새 토큰 3개까지 돌려 같은지 출력한다(단언하지 않음)."""
import gc

import pytest
import torch

from microscope.engine import chat, config, generate, weights
from microscope.engine.sampler import SamplingConfig

pytestmark = pytest.mark.model


def tensors_of(res) -> dict:
    return {k: t for r in res.recorders for k, (_l, t) in r.tensors.items()}


def diff_names(a: dict, b: dict) -> list[str]:
    assert list(a) == list(b)
    return [k for k in a if not torch.equal(a[k], b[k])]


@pytest.fixture(scope="module")
def engine():
    cfg = config.load_config()
    w = weights.load_all(cfg)
    yield cfg, w, chat.build_prompt().token_ids, config.eos_token_ids()
    del w
    gc.collect()


def gen(engine, max_new: int, sampling: SamplingConfig):
    cfg, w, ids, eos = engine
    return generate.generate(ids, w, cfg, sampling, max_new, eos, "f32", capture=True)


def test_v12_two_runs_bit_identical(engine):
    s = SamplingConfig()
    a, b = gen(engine, 8, s), gen(engine, 8, s)
    ta, tb = tensors_of(a), tensors_of(b)
    bad = diff_names(ta, tb)
    print(f"V12: 텐서 {len(ta)}개 중 다른 것 {len(bad)}개 {bad[:5]} | 토큰 {[t['id'] for t in a.tokens]}")
    assert bad == []
    assert [t["id"] for t in a.tokens] == [t["id"] for t in b.tokens]
    assert [r.mlp_summary for r in a.recorders] == [r.mlp_summary for r in b.recorders]


def test_v12_sampling_same_seed_identical(engine):
    s = SamplingConfig(temperature=0.8, top_k=20, top_p=0.9, seed=42)
    a, b = gen(engine, 4, s), gen(engine, 4, s)
    assert [t["id"] for t in a.tokens] == [t["id"] for t in b.tokens]
    assert [t["sampling"]["u"] for t in a.tokens] == [t["sampling"]["u"] for t in b.tokens]


def test_v12_info_threads_6_vs_8(engine):
    old = torch.get_num_threads()
    try:
        torch.set_num_threads(6)
        a = gen(engine, 3, SamplingConfig())
        torch.set_num_threads(8)
        b = gen(engine, 3, SamplingConfig())
    finally:
        torch.set_num_threads(old)
    bad = diff_names(tensors_of(a), tensors_of(b))
    print(f"V12 정보: 스레드 6 vs 8, 새 토큰 3개, 텐서 {len(tensors_of(a))}개 중 다른 것 {len(bad)}개 {bad[:5]} "
          f"| 토큰 같음 {[t['id'] for t in a.tokens] == [t['id'] for t in b.tokens]}")
