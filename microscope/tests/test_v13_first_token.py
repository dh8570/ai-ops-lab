"""V13 첫 토큰: Prefill 끝에서 뽑은 첫 토큰(탐욕, f32·bf16 경로 모두)이 M2-a V2의 1등(서, id 26698)과 같다."""
import gc

import pytest

from microscope.engine import chat, config, generate, weights
from microscope.engine.sampler import SamplingConfig

pytestmark = pytest.mark.model
EXPECTED = 26698        # '서' (M2-a V2, HF eager 1등)


def test_v13_first_token():
    cfg = config.load_config()
    tok = chat.tokenizer()
    w = weights.load_all(cfg)
    ids = chat.build_prompt(tok=tok).token_ids
    out = {}
    for path in generate.LOGITS_PATHS:
        r = generate.generate(ids, w, cfg, SamplingConfig(), 1, config.eos_token_ids(), path, capture=False)
        out[path] = r.tokens[0]
    del w
    gc.collect()
    print("V13:", {p: (t["id"], tok.decode([t["id"]]), t["source"]) for p, t in out.items()})
    assert all(t["id"] == EXPECTED and t["source"] == "prefill" for t in out.values())
