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
    assert set(s) == {"step", "layer", "gate", "up", "act", "act_top8"}       # M2-b: step 필드 추가(Prefill은 null)
    assert s["step"] is None
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


def two_phase_run(tmp_path):
    """Prefill(T=3) + Decode 2단계를 흉내 낸 가짜 기록."""
    torch.manual_seed(2)
    pre = capture.Recorder("prefill")
    pre("k", 0, torch.randn(2, 3, 4).to(torch.bfloat16))
    pre("v", 0, torch.randn(2, 3, 4).to(torch.bfloat16))
    steps = []
    for j in (1, 2):
        r = capture.Recorder("decode", step=j)
        r("k", 0, torch.randn(2, 1, 4).to(torch.bfloat16))
        r("v", 0, torch.randn(2, 1, 4).to(torch.bfloat16))
        r("attn", 0, torch.rand(3, 1, 3 + j).to(torch.bfloat16))
        r("mlp.act", 0, torch.randn(1, 16).to(torch.bfloat16))
        r("logits_last_f32", None, torch.randn(7))
        steps.append(r)
    d = tmp_path / "r-two"
    capture.write_run(d, {"schema_version": 0}, [pre, *steps])
    return d, pre, steps


def test_decode_file_offsets_and_names(tmp_path):
    d, _, _ = two_phase_run(tmp_path)
    m = capture.load_manifest(d)
    names = [e["name"] for e in m["tensors"]]
    assert len(names) == len(set(names))
    assert "D2.L0.attn" in names and "D1.logits_last_f32" in names
    for fname in capture.BIN_FILES.values():
        es = [e for e in m["tensors"] if e["file"] == fname]
        pos = 0
        for e in es:
            assert e["offset"] == pos
            pos += e["nbytes"]
        assert pos == (d / fname).stat().st_size
    e = next(x for x in m["tensors"] if x["name"] == "D2.L0.attn")
    assert e["phase"] == "decode" and e["step"] == 2 and e["layer"] == 0 and e["shape"] == [3, 1, 5]
    assert next(x for x in m["tensors"] if x["name"] == "D1.logits_last_f32")["layer"] is None
    assert [s["step"] for s in m["mlp_summary"]] == [1, 2]


def test_read_kv_cache(tmp_path):
    d, pre, steps = two_phase_run(tmp_path)
    k, v = capture.read_kv_cache(d, 0, 2)
    want_k = torch.cat([pre.tensors["L0.k"][1], steps[0].tensors["D1.L0.k"][1], steps[1].tensors["D2.L0.k"][1]], 1)
    assert k.shape == (2, 5, 4) and np.array_equal(k, want_k.float().numpy())
    k0, _ = capture.read_kv_cache(d, 0, 0)
    assert k0.shape == (2, 3, 4)


def test_old_manifest_without_file_field(run_dir):
    """M2-a 기록(file 필드 없음)도 prefill.bin으로 읽는다."""
    d, rec = run_dir
    m = capture.load_manifest(d)
    for e in m["tensors"]:
        e.pop("file")
    (d / "manifest.json").write_text(json.dumps(m))
    t = rec.tensors["L0.q"][1].to(torch.float32).numpy()
    assert np.array_equal(capture.read_tensor(d, "L0.q"), t)


def test_recorder_phase_step_mismatch():
    with pytest.raises(ValueError):
        capture.Recorder("decode")
    with pytest.raises(ValueError):
        capture.Recorder("prefill", step=1)
