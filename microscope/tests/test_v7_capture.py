"""V7: 저장 형식. 작은 가짜 텐서로 쓰고 읽어 값이 같은지, offset·nbytes가 파일과 맞는지, 이름이 중복되지 않는지."""
import json
import re

import numpy as np
import pytest
import torch

from microscope.engine import capture


def fake_recorder() -> capture.Recorder:
    torch.manual_seed(1)
    rec = capture.Recorder("prefill")
    rec("embed", None, torch.randn(5, 8).to(torch.bfloat16))
    rec("q", 0, torch.randn(4, 5, 3).to(torch.bfloat16))
    rec("scores", 0, torch.tensor([[0.5, torch.finfo(torch.bfloat16).min], [1.0, -2.0]], dtype=torch.bfloat16))
    rec("mlp.gate", 0, torch.randn(5, 16).to(torch.bfloat16))
    rec("mlp.up", 0, torch.randn(5, 16).to(torch.bfloat16))
    rec("mlp.act", 0, torch.randn(5, 16).to(torch.bfloat16))
    rec("logits_last_f32", None, torch.randn(11))
    return rec


@pytest.fixture
def run_dir(tmp_path):
    rec = fake_recorder()
    d = tmp_path / "r-test"
    rec.write(d, {"schema_version": 0, "run_id": "r-test"})
    return d, rec


def test_roundtrip_values(run_dir):
    d, rec = run_dir
    for key, (_layer, t) in rec.tensors.items():
        got = capture.read_tensor(d, key)
        assert got.dtype == np.float32 and got.shape == tuple(t.shape)
        assert np.array_equal(got, t.to(torch.float32).numpy()), key


def test_bf16_bits_roundtrip(run_dir):
    """bf16 → uint16 원본 비트 → float32 → bf16이 원래 비트와 같다(최솟값·음수 포함)."""
    d, rec = run_dir
    t = rec.tensors["L0.scores"][1]
    back = torch.from_numpy(capture.read_tensor(d, "L0.scores")).to(torch.bfloat16)
    assert torch.equal(back.view(torch.int16), t.view(torch.int16))
    raw = (d / capture.BIN_NAME).read_bytes()
    e = next(x for x in capture.load_manifest(d)["tensors"] if x["name"] == "L0.scores")
    first = int.from_bytes(raw[e["offset"]:e["offset"] + 2], "little")
    assert first == int(t.view(torch.int16)[0, 0]) & 0xFFFF            # 리틀 엔디안 원본 비트


def test_offsets_and_size(run_dir):
    d, _ = run_dir
    m = capture.load_manifest(d)
    pos = 0
    for e in m["tensors"]:
        assert e["offset"] == pos
        itemsize = {"bf16": 2, "f32": 4}[e["dtype"]]
        assert e["nbytes"] == int(np.prod(e["shape"])) * itemsize
        pos += e["nbytes"]
    assert pos == (d / capture.BIN_NAME).stat().st_size


def test_names_unique_and_mlp_not_stored(run_dir):
    d, _ = run_dir
    m = capture.load_manifest(d)
    names = [e["name"] for e in m["tensors"]]
    assert len(names) == len(set(names))
    assert not any("mlp." in n for n in names)
    s = m["mlp_summary"][0]
    assert set(s) == {"layer", "gate", "up", "act", "act_top8"}
    assert len(s["act_top8"]) == 5 and all(len(row) == capture.TOPK_PER_TOKEN for row in s["act_top8"])


def test_duplicate_name_rejected():
    rec = capture.Recorder()
    rec("embed", None, torch.zeros(2, 2))
    with pytest.raises(ValueError):
        rec("embed", None, torch.zeros(2, 2))


def test_slicing(run_dir):
    d, rec = run_dir
    t = rec.tensors["L0.q"][1].to(torch.float32).numpy()
    assert np.array_equal(capture.read_tensor(d, "L0.q", (2,)), t[2])                   # 한 헤드
    assert np.array_equal(capture.read_tensor(d, "L0.q", (2, 4)), t[2, 4])              # 한 헤드의 한 토큰
    assert np.array_equal(capture.read_tensor(d, "L0.q", (slice(None), 1)), t[:, 1])    # 모든 헤드의 한 토큰


def test_stats_no_overflow_with_mask_value(run_dir):
    d, _ = run_dir
    e = next(x for x in capture.load_manifest(d)["tensors"] if x["name"] == "L0.scores")
    assert all(np.isfinite(v) for v in e["stats"].values())
    assert e["stats"]["min"] == float(torch.finfo(torch.bfloat16).min)


def test_run_id_format():
    assert re.fullmatch(r"r-\d{8}-\d{6}-[0-9a-f]{4}", capture.new_run_id())


def test_manifest_is_json(run_dir):
    d, _ = run_dir
    assert json.loads((d / "manifest.json").read_text())["timing"]["capture_write_s"] >= 0
