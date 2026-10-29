"""캡처 파일 형식 v0. 실행 하나 = 폴더 하나(manifest.json + tensors/prefill.bin [+ tensors/decode.bin]).
설명은 docs/microscope/capture-format.md."""
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import torch

SCHEMA_VERSION = 0
BIN_NAME = "tensors/prefill.bin"            # file 필드가 없는 M2-a 기록은 이 파일로 읽는다
BIN_FILES = {"prefill": "tensors/prefill.bin", "decode": "tensors/decode.bin"}
# MLP 안쪽 값은 통째로 저장하지 않고 요약만 남긴다(층당 [T, intermediate_size]라 크다)
MLP_INNER = {"mlp.gate": "gate", "mlp.up": "up", "mlp.act": "act"}
TOPK_PER_TOKEN = 8
_DTYPES = {torch.bfloat16: ("bf16", "<u2"), torch.float32: ("f32", "<f4")}
_NP = {"bf16": "<u2", "f32": "<f4"}


def new_run_id() -> str:
    return datetime.now(UTC).strftime("r-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def stats(t: torch.Tensor) -> dict:
    """float32로 바꾼 값으로 계산한다. 누적은 float64(마스크 값 -3.39e38처럼 큰 수의 제곱이 넘치지 않게)."""
    x = t.to(torch.float32).to(torch.float64)
    return {"min": float(x.min()), "max": float(x.max()), "mean": float(x.mean()),
            "std": float(x.std(unbiased=False)), "l2": float(x.pow(2).sum().sqrt())}


def tensor_name(name: str, layer: int | None, step: int | None = None) -> str:
    base = name if layer is None else f"L{layer}.{name}"
    return base if step is None else f"D{step}.{base}"


class Recorder:
    """layer.py·model.py의 capture 콜백 하나 = 한 단계(Prefill 또는 Decode 단계 j).
    텐서는 복사해 모아 두고, write_run()이 한 번에 쓴다."""

    def __init__(self, phase: str = "prefill", step: int | None = None):
        if phase not in BIN_FILES or (phase == "decode") != (step is not None):
            raise ValueError(f"phase {phase}와 step {step}이 맞지 않는다")
        self.phase = phase
        self.step = step
        self.file = BIN_FILES[phase]
        self.tensors: dict[str, tuple[int | None, torch.Tensor]] = {}
        self.mlp_summary: dict[int, dict] = {}

    def __call__(self, name: str, layer: int | None, t: torch.Tensor) -> None:
        if name in MLP_INNER:
            self._summarize(MLP_INNER[name], layer, t)
            return
        key = tensor_name(name, layer, self.step)
        if key in self.tensors:
            raise ValueError(f"텐서 이름 중복: {key}")
        if t.dtype not in _DTYPES:
            raise ValueError(f"{key}: 저장할 수 없는 자료형 {t.dtype}")
        self.tensors[key] = (layer, t.detach().contiguous().clone())

    def _summarize(self, kind: str, layer: int, t: torch.Tensor) -> None:
        s = self.mlp_summary.setdefault(layer, {"step": self.step, "layer": layer})
        s[kind] = stats(t)
        if kind == "act":          # SiLU(gate)×up: down_proj로 들어가는 값. 토큰마다 |값|이 큰 상위 8개 (위치, 값)
            idx = t.to(torch.float32).abs().topk(TOPK_PER_TOKEN, dim=-1).indices
            val = t.to(torch.float32).gather(-1, idx)
            s["act_top8"] = [[[int(i), float(v)] for i, v in zip(ri, rv, strict=True)]
                             for ri, rv in zip(idx.tolist(), val.tolist(), strict=True)]

    def write(self, run_dir: Path, manifest: dict) -> dict:
        """이 Recorder 하나만 쓴다(M2-a와 같은 사용법)."""
        return write_run(run_dir, manifest, [self])


def write_run(run_dir: Path, manifest: dict, recorders: list[Recorder]) -> dict:
    """Recorder들을 순서대로 단계별 파일에 이어 쓰고 manifest.json을 쓴다.
    반환: {seconds, bytes: {파일: 바이트}}."""
    t0 = time.perf_counter()
    (run_dir / "tensors").mkdir(parents=True, exist_ok=True)
    entries, sizes, handles = [], {}, {}
    try:
        for rec in recorders:
            if rec.file not in handles:
                handles[rec.file] = open(run_dir / rec.file, "wb")
                sizes[rec.file] = 0
            f = handles[rec.file]
            for key, (layer, t) in rec.tensors.items():
                dname, npdt = _DTYPES[t.dtype]
                raw = t.view(torch.int16) if t.dtype == torch.bfloat16 else t        # bf16은 원본 비트(uint16)
                data = raw.numpy().astype(npdt, copy=False).tobytes()
                f.write(data)
                entries.append({"name": key, "phase": rec.phase, "step": rec.step, "layer": layer,
                                "shape": list(t.shape), "dtype": dname, "file": rec.file,
                                "offset": sizes[rec.file], "nbytes": len(data), "stats": stats(t)})
                sizes[rec.file] += len(data)
    finally:
        for f in handles.values():
            f.close()
    summary = [r.mlp_summary[k] for r in recorders for k in sorted(r.mlp_summary)]
    manifest = {**manifest, "tensors": entries, "mlp_summary": summary}
    sec = time.perf_counter() - t0
    manifest.setdefault("timing", {})["capture_write_s"] = round(sec, 3)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return {"seconds": sec, "bytes": sizes}


def load_manifest(run_dir: str | Path) -> dict:
    return json.loads((Path(run_dir) / "manifest.json").read_text())


def read_tensor(run_dir: str | Path, name: str, index=None, manifest: dict | None = None) -> np.ndarray:
    """캡처 텐서를 float32 numpy로 읽는다. index는 numpy 슬라이싱 그대로(예: (3, slice(None), 5)).
    파일 전체를 읽지 않고 memmap으로 필요한 부분만 읽는다."""
    m = manifest or load_manifest(run_dir)
    e = next((x for x in m["tensors"] if x["name"] == name), None)
    if e is None:
        raise KeyError(name)
    mm = np.memmap(Path(run_dir) / e.get("file", BIN_NAME), dtype=_NP[e["dtype"]], mode="r", offset=e["offset"],
                   shape=tuple(e["shape"]))
    a = np.array(mm if index is None else mm[index])
    if e["dtype"] == "bf16":
        a = (a.astype(np.uint32) << 16).view(np.float32)        # bf16 비트는 float32의 위 16비트와 같다
    return a.astype(np.float32, copy=False)


def read_kv_cache(run_dir: str | Path, layer: int, step: int, manifest: dict | None = None):
    """Decode 단계 step까지 마친 뒤 층 layer의 KV 캐시 (키, 값), 각각 [Hk, T + step, D] float32.
    = Prefill의 L{l}.k·L{l}.v 뒤에 D1..D{step}의 새 k·v를 이어 붙인 것. step 0이면 Prefill만."""
    m = manifest or load_manifest(run_dir)
    ks = [read_tensor(run_dir, f"L{layer}.k", manifest=m)]
    vs = [read_tensor(run_dir, f"L{layer}.v", manifest=m)]
    for j in range(1, step + 1):
        ks.append(read_tensor(run_dir, f"D{j}.L{layer}.k", manifest=m))
        vs.append(read_tensor(run_dir, f"D{j}.L{layer}.v", manifest=m))
    return np.concatenate(ks, axis=1), np.concatenate(vs, axis=1)
