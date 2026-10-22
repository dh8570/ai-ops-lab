import json
import os


class LogReader:
    """파일 끝에 새로 추가된 줄만 읽는다 (offset 기반).

    - 줄바꿈이 없는 마지막 조각은 아직 쓰는 중일 수 있어 다음 호출로 넘긴다.
    - 파일이 잘리거나(크기 < offset) 교체되면(inode 변경) 처음부터 다시 읽는다.
    """

    def __init__(self, path: str, start_at_end: bool = True):
        self.path = path
        self.offset = 0
        self.inode = None
        self.partial = b""
        if start_at_end and os.path.exists(path):
            st = os.stat(path)
            self.offset, self.inode = st.st_size, st.st_ino

    def read_new(self) -> tuple[list[dict], int]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return [], 0
        if st.st_ino != self.inode or st.st_size < self.offset:
            self.offset, self.partial, self.inode = 0, b"", st.st_ino

        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)

        chunks = (self.partial + data).split(b"\n")
        self.partial = chunks.pop()            # 미완성 줄은 보관

        records, bad = [], 0
        for raw in chunks:
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                bad += 1                       # 깨진 줄은 세기만 하고 건너뜀
        return records, bad

    def snapshot(self) -> dict:
        return {"offset": self.offset, "inode": self.inode}

    def restore(self, snap: dict) -> None:
        """같은 파일이고 잘리지 않았을 때만 이어서 읽는다. 아니면 생성자 기본값(파일 끝)을 유지."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        if st.st_ino == snap.get("inode") and st.st_size >= snap.get("offset", 0):
            self.offset, self.inode, self.partial = snap["offset"], snap["inode"], b""
