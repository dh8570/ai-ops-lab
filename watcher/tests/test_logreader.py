
from logreader import LogReader


# 생성 시점에 이미 있던 줄은 읽지 않고, 이후 append 만 반환한다.
def test_skips_preexisting_lines_at_creation(tmp_path):
    log = tmp_path / "app.jsonl"
    log.write_text('{"path":"/a"}\n', encoding="utf-8")
    reader = LogReader(str(log), start_at_end=True)
    log.write_text(log.read_text(encoding="utf-8") + '{"path":"/b"}\n', encoding="utf-8")
    records, bad = reader.read_new()
    assert bad == 0
    assert len(records) == 1
    assert records[0]["path"] == "/b"


# 줄바꿈 없이 끊긴 JSON 조각은 partial에 보관했다가 다음 read에서 완성한다.
def test_partial_line_completed_on_next_read(tmp_path):
    log = tmp_path / "app.jsonl"
    log.write_bytes(b'{"path":')
    reader = LogReader(str(log), start_at_end=False)
    assert reader.read_new() == ([], 0)
    with open(log, "ab") as f:
        f.write(b'"/c"}\n')
    records, bad = reader.read_new()
    assert bad == 0
    assert records == [{"path": "/c"}]


# 파일이 잘리면(offset보다 작아지면) 처음부터 다시 읽는다.
def test_reread_from_start_when_file_truncated(tmp_path):
    log = tmp_path / "app.jsonl"
    log.write_text('{"path":"old"}\n' + ("x" * 200), encoding="utf-8")
    reader = LogReader(str(log), start_at_end=False)
    reader.read_new()
    log.write_text('{"path":"new"}\n', encoding="utf-8")
    records, bad = reader.read_new()
    assert bad == 0
    assert len(records) == 1
    assert records[0]["path"] == "new"


# 깨진 JSON 한 줄은 bad 로만 세고 records 에 넣지 않는다.
def test_bad_json_line_counted(tmp_path):
    log = tmp_path / "app.jsonl"
    log.write_text("not-json\n", encoding="utf-8")
    reader = LogReader(str(log), start_at_end=False)
    records, bad = reader.read_new()
    assert records == []
    assert bad == 1


# snapshot 후 재시작(새 LogReader + restore)하면 그 사이에 쓴 줄을 읽는다.
def test_restore_after_snapshot_reads_lines_since_checkpoint(tmp_path):
    log = tmp_path / "app.jsonl"
    log.write_text("", encoding="utf-8")
    reader = LogReader(str(log), start_at_end=False)
    reader.read_new()
    snap = reader.snapshot()
    log.write_text('{"path":"after-restart"}\n', encoding="utf-8")
    reader2 = LogReader(str(log), start_at_end=True)
    reader2.restore(snap)
    records, bad = reader2.read_new()
    assert bad == 0
    assert records == [{"path": "after-restart"}]
