"""KV 캐시: 층별로 RoPE 뒤 키·값 [1,Hk,Tk,D]를 들고 있다. transformers DynamicLayer.update와 같이
새 키·값을 torch.cat(dim=-2)로 뒤에 이어 붙인다."""
import torch


class KVCache:
    def __init__(self, keys: list[torch.Tensor], values: list[torch.Tensor]):
        if len(keys) != len(values):
            raise ValueError("키와 값의 층 수가 다르다")
        self.keys = keys
        self.values = values

    @classmethod
    def from_prefill(cls, keys: list[torch.Tensor], values: list[torch.Tensor]) -> "KVCache":
        """Prefill 결과(층별 [Hk,T,D], 배치 차원 없음)에서 만든다."""
        return cls([k.unsqueeze(0) for k in keys], [v.unsqueeze(0) for v in values])

    def __len__(self) -> int:
        """캐시에 든 토큰 수(= 다음 토큰의 위치)."""
        return self.keys[0].shape[-2]

    def get(self, layer: int) -> tuple[torch.Tensor, torch.Tensor]:
        return self.keys[layer], self.values[layer]

    def append(self, layer: int, k: torch.Tensor, v: torch.Tensor) -> None:
        """k, v: 새 토큰의 [1,Hk,1,D]."""
        self.keys[layer] = torch.cat([self.keys[layer], k], dim=-2)
        self.values[layer] = torch.cat([self.values[layer], v], dim=-2)
