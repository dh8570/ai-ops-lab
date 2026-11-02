"""V11 뽑기 단위 시험(모델 없이). 알려진 작은 로짓으로 sampler.py의 규칙을 확인한다."""
import math

import pytest
import torch

from microscope.engine import sampler
from microscope.engine.sampler import Sampler, SamplingConfig

LOGITS = torch.tensor([1.0, 3.0, 2.0, 3.0, 0.5], dtype=torch.float32)     # 1번과 3번이 같은 최댓값


def test_i_greedy_argmax_lowest_index():
    tid, rec = Sampler(SamplingConfig(temperature=0)).sample(LOGITS)
    assert tid == 1 and rec["mode"] == "greedy" and rec["u"] is None


def test_ii_top_k_1_equals_greedy():
    s = Sampler(SamplingConfig(temperature=0.7, top_k=1, seed=3))
    for _ in range(20):
        tid, rec = s.sample(LOGITS)
        assert tid == 1                                    # 같은 확률이면 번호 작은 1번이 앞에 정렬된다
        assert rec["kept_after_top_k"] == 1 and rec["candidates"][0]["prob"] == 1.0


def test_iii_tiny_top_p_keeps_first():
    tid, rec = Sampler(SamplingConfig(temperature=1.0, top_p=1e-9, seed=0)).sample(LOGITS)
    assert rec["kept_after_top_p"] == 1 and tid == 1


def test_iv_probabilities_match_hand_calculation():
    """T=2, top_k=3: 정렬 [3,3,2] → exp(1.5),exp(1.5),exp(1.0)를 합 1로 = 0.383652, 0.383652, 0.232697.
    top_p=0.7: 앞 누적 0, 0.383652 < 0.7 → 남음, 0.767303 ≥ 0.7 → 빠짐 → 2개."""
    s = Sampler(SamplingConfig(temperature=2.0, top_k=3, top_p=0.7, seed=1))
    _, rec = s.sample(LOGITS)
    a, b = math.exp(1.5), math.exp(1.0)
    assert rec["kept_after_temperature"] == 5 and rec["kept_after_top_k"] == 3
    assert a / (2 * a + b) == pytest.approx(0.383652, abs=1e-6)
    assert rec["kept_after_top_p"] == 2
    assert [c["id"] for c in rec["candidates"]] == [1, 3]
    assert rec["candidates"][0]["prob"] == pytest.approx(0.5, abs=1e-6)
    assert rec["candidates"][1]["cum"] == pytest.approx(1.0, abs=1e-6)
    p, _ = sampler.probs_sorted(LOGITS, 2.0)
    z = sum(math.exp(x / 2) for x in LOGITS.tolist())
    assert p[0].item() == pytest.approx(math.exp(1.5) / z, abs=1e-6)
    assert p[4].item() == pytest.approx(math.exp(0.25) / z, abs=1e-6)


def run_seq(seed: int, n: int = 30) -> list[int]:
    s = Sampler(SamplingConfig(temperature=1.5, top_k=4, top_p=0.95, seed=seed))
    return [s.sample(LOGITS)[0] for _ in range(n)]


def test_v_same_seed_same_sequence():
    assert run_seq(42) == run_seq(42)
    assert run_seq(42) != run_seq(7)          # 이 seed 쌍에서는 달라짐(일반적으로는 '달라질 수 있다')


def test_vi_frequency_matches_theory():
    s = Sampler(SamplingConfig(temperature=1.0, seed=2026))
    n = 20000
    counts = torch.zeros(5)
    for _ in range(n):
        counts[s.sample(LOGITS)[0]] += 1
    theory = torch.softmax(LOGITS.double(), 0)
    err = (counts.double() / n - theory).abs().max().item()
    print(f"V11-vi 빈도 {(counts / n).tolist()} 이론 {[round(x, 4) for x in theory.tolist()]} 최대 오차 {err:.4f}")
    assert err < 0.01


def test_vii_boundary_rule():
    cum = torch.tensor([0.25, 0.5, 1.0], dtype=torch.float64)
    assert sampler.choose(cum, 0.0) == 0
    assert sampler.choose(cum, 0.25) == 1           # u가 경계와 같으면 '처음으로 u보다 큰' 다음 토큰
    assert sampler.choose(cum, 0.2499999) == 0
    assert sampler.choose(cum, 0.5) == 2
    assert sampler.choose(torch.tensor([0.5, 0.9999999], dtype=torch.float64), 0.99999995) == 1   # 반올림 부족 → 마지막


def test_viii_nan_rejected():
    bad = LOGITS.clone()
    bad[2] = float("nan")
    with pytest.raises(ValueError):
        Sampler(SamplingConfig(temperature=0)).sample(bad)
    with pytest.raises(ValueError):
        Sampler(SamplingConfig(temperature=1.0, seed=0)).sample(bad)


def test_config_validation():
    with pytest.raises(ValueError):
        SamplingConfig(temperature=0.5)              # seed 없음
    with pytest.raises(ValueError):
        SamplingConfig(temperature=-1)
    with pytest.raises(ValueError):
        SamplingConfig(top_k=0)
    with pytest.raises(ValueError):
        SamplingConfig(top_p=0)
