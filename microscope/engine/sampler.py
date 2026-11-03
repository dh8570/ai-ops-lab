"""뽑기(sampling). 우리 자체 구현이다("microscope-v0"). Ollama·llama.cpp·HF의 구현과 같다고 주장하지 않는다.

순서(단순화): temperature → top_k → top_p → 난수로 고르기. 실제 구현마다 순서와 세부 규칙이 다르다.
1) p = softmax(logits / temperature)를 float64로 계산
2) 확률 큰 순서(같으면 번호 작은 순서)로 정렬
3) top_k가 있으면 앞의 k개만 남김
4) 남은 것을 합 1로 맞춘 뒤, top_p가 있으면 "그 앞까지의 누적이 top_p 미만"인 토큰만 남김(첫 토큰은 항상 남음)
5) 남은 것을 합 1로 다시 맞춤
6) u = torch.rand(1, float64, generator)로 뽑아 누적이 처음으로 u보다 커지는 토큰을 고름
temperature == 0이면 탐욕: argmax(같은 값이면 번호가 작은 토큰), 난수를 쓰지 않는다.
"""
from collections.abc import Callable
from dataclasses import dataclass

import torch

SAMPLER_NAME = "microscope-v0"
TOP_CANDIDATES = 50


@dataclass(frozen=True)
class SamplingConfig:
    temperature: float = 0.0
    top_k: int | None = None
    top_p: float | None = None
    seed: int | None = None

    def __post_init__(self):
        if not self.temperature >= 0:
            raise ValueError("temperature는 0 이상이어야 한다")
        if self.top_k is not None and (int(self.top_k) != self.top_k or self.top_k < 1):
            raise ValueError("top_k는 1 이상의 정수여야 한다")
        if self.top_p is not None and not (0 < self.top_p <= 1):
            raise ValueError("top_p는 0 초과 1 이하여야 한다")
        if self.temperature > 0 and self.seed is None:
            raise ValueError("뽑기(temperature > 0)에는 seed가 필요하다")

    @property
    def greedy(self) -> bool:
        return self.temperature == 0

    def as_dict(self) -> dict:
        return {"temperature": self.temperature, "top_k": self.top_k, "top_p": self.top_p, "seed": self.seed,
                "sampler": SAMPLER_NAME}


def check_logits(logits: torch.Tensor) -> None:
    if torch.isnan(logits).any():
        raise ValueError("로짓에 NaN이 있다")
    if torch.isposinf(logits).any():
        raise ValueError("로짓에 +inf가 있다")


def argmax_lowest(logits: torch.Tensor) -> int:
    """최댓값의 번호. 같은 값이 여럿이면 가장 작은 번호."""
    check_logits(logits)
    return int((logits == logits.max()).nonzero()[0, 0])


def greedy_record(tid: int, text: str, mode: str = "greedy") -> dict:
    """탐욕 선택의 기록(난수 없음, 후보는 고른 토큰 하나)."""
    return {"mode": mode, "u": None, "kept_after_temperature": None, "kept_after_top_k": None,
            "kept_after_top_p": None, "candidates": [{"id": tid, "text": text, "prob": 1.0, "cum": 1.0}],
            "chosen_index": 0}


def choose(cum: torch.Tensor, u: float) -> int:
    """누적 확률 cum(오름차순)에서 처음으로 u보다 큰 자리. 반올림으로 마지막 누적이 u 이하이면 마지막 자리."""
    hit = (cum > u).nonzero()
    return int(hit[0, 0]) if len(hit) else len(cum) - 1


def probs_sorted(logits: torch.Tensor, temperature: float) -> tuple[torch.Tensor, torch.Tensor]:
    """1)~2): float64 softmax(logits / temperature)를 확률 큰 순서(같으면 번호 작은 순서)로 정렬."""
    p = torch.softmax(logits.to(torch.float64) / temperature, dim=-1)
    order = torch.sort(p, descending=True, stable=True)          # stable: 같은 확률이면 원래 순서(번호 작은 것) 먼저
    return order.values, order.indices


def filter_probs(p: torch.Tensor, top_k: int | None, top_p: float | None) -> tuple[torch.Tensor, int, int]:
    """3)~5): 정렬된 확률 p에 top_k, top_p를 적용하고 합 1로 맞춘다. 반환: (남은 확률, top_k 뒤 수, top_p 뒤 수)."""
    if top_k is not None:
        p = p[:top_k]
    p = p / p.sum()
    n_k = len(p)
    if top_p is not None:
        before = torch.cumsum(p, 0) - p                           # 그 앞까지의 누적
        p = p[before < top_p]                                     # 첫 토큰은 before = 0 < top_p 이므로 항상 남는다
    p = p / p.sum()
    return p, n_k, len(p)


class Sampler:
    """실행 하나에 하나. seed로 만든 Generator를 단계마다 이어서 쓴다."""

    def __init__(self, cfg: SamplingConfig, decode: Callable[[int], str] | None = None):
        self.cfg = cfg
        self.decode = decode or (lambda i: "")
        self.gen = None if cfg.greedy else torch.Generator(device="cpu").manual_seed(int(cfg.seed))

    def sample(self, logits: torch.Tensor) -> tuple[int, dict]:
        """logits: float32 [vocab]. 반환: (고른 토큰 번호, 기록)."""
        check_logits(logits)
        c = self.cfg
        if c.greedy:
            tid = argmax_lowest(logits)
            return tid, greedy_record(tid, self.decode(tid))
        p, idx = probs_sorted(logits, c.temperature)
        n_t = len(p)
        p, n_k, n_p = filter_probs(p, c.top_k, c.top_p)
        idx = idx[: len(p)]
        cum = torch.cumsum(p, 0)
        u = float(torch.rand(1, dtype=torch.float64, generator=self.gen))
        j = choose(cum, u)
        tid = int(idx[j])
        cands = [{"id": int(idx[i]), "text": self.decode(int(idx[i])), "prob": float(p[i]), "cum": float(cum[i])}
                 for i in range(min(TOP_CANDIDATES, len(p)))]
        rec = {"mode": "sample", "u": u, "kept_after_temperature": n_t, "kept_after_top_k": n_k,
               "kept_after_top_p": n_p, "candidates": cands, "chosen_index": j}
        return tid, rec
