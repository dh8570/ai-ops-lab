"""검증용 '공식 구현'(transformers 5.19.0 Qwen2, eager) 실행 도우미. 런타임 엔진(engine/)은 이 파일을 쓰지 않는다."""
import numpy as np
import torch
from transformers import AutoModelForCausalLM

from .engine.config import model_dir


def load_hf(dtype: torch.dtype = torch.bfloat16):
    m = AutoModelForCausalLM.from_pretrained(str(model_dir()), dtype=dtype, attn_implementation="eager")
    m.eval()
    return m


@torch.no_grad()
def hf_prefill(hf, ids: list[int]) -> dict:
    """HF eager로 Prefill. 반환(배치 차원 제거):
    layer_out[l] [T,H](층 출력, forward hook), hidden_states(29개: 0=임베딩, 1..L-1=층 0..L-2 출력, L=최종 정규화),
    attentions[l] [Hq,T,T], keys/values[l] [Hk,T,D], logits_last [vocab]."""
    layer_out: dict[int, torch.Tensor] = {}
    hooks = [layer.register_forward_hook(lambda _m, _i, o, i=i: layer_out.__setitem__(i, o[0].clone()))
             for i, layer in enumerate(hf.model.layers)]
    try:
        out = hf(input_ids=torch.tensor([ids]), use_cache=True, output_hidden_states=True, output_attentions=True)
    finally:
        for h in hooks:
            h.remove()
    pkv = out.past_key_values
    return {
        "layer_out": [layer_out[i] for i in range(len(hf.model.layers))],
        "hidden_states": [h[0] for h in out.hidden_states],
        "attentions": [a[0] for a in out.attentions],
        "keys": [layer.keys[0] for layer in pkv.layers],
        "values": [layer.values[0] for layer in pkv.layers],
        "logits_last": out.logits[0, -1].clone(),
    }


def _f64(x) -> torch.Tensor:
    return x.double() if isinstance(x, torch.Tensor) else torch.from_numpy(np.asarray(x)).double()


def diff(ours, ref) -> dict:
    """최대 절대 차이, 평균 절대 차이, 상대 L2 오차(‖ours−ref‖ / ‖ref‖). float64로 계산."""
    a, b = _f64(ours), _f64(ref)
    if a.shape != b.shape:
        raise ValueError(f"모양이 다르다: {tuple(a.shape)} vs {tuple(b.shape)}")
    d = (a - b).abs()
    nb = float(b.pow(2).sum().sqrt())
    return {"max_abs": float(d.max()), "mean_abs": float(d.mean()),
            "rel_l2": float(d.pow(2).sum().sqrt()) / nb if nb else float("nan"),
            "bit_equal": bool(torch.equal(a, b))}
