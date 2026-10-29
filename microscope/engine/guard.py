"""메모리 감시와 시간 상한. 스파이크 03_measure.py와 같은 방식(감시 스레드가 0.5초마다 확인, 넘으면 즉시 종료)."""
import math
import os
import sys
import threading
import time

MEM_FLOOR_GIB = 8.0


def mem_available_gib() -> float:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    return math.nan


def proc_mem_gib() -> dict:
    """VmRSS(지금), VmHWM(최대치), RssAnon(직접 할당), RssFile(파일 매핑)을 GiB로."""
    out = {}
    with open("/proc/self/status") as f:
        for line in f:
            k = line.split(":")[0]
            if k in ("VmRSS", "VmHWM", "RssAnon", "RssFile"):
                out[k] = round(int(line.split()[1]) / 1024 / 1024, 3)
    out["MemAvailable"] = round(mem_available_gib(), 3)
    return out


def start_guard(limit_s: float, floor_gib: float = MEM_FLOOR_GIB, label: str = "microscope") -> threading.Thread:
    """MemAvailable < floor_gib 이면 종료코드 97, 시간 상한을 넘으면 98로 프로세스를 즉시 끝낸다."""
    t_end = time.monotonic() + limit_s

    def run():
        while True:
            a = mem_available_gib()
            if a < floor_gib:
                print(f"!!! [{label}] MemAvailable {a:.2f} GiB < {floor_gib} GiB: 즉시 중단",
                      file=sys.stderr, flush=True)
                os._exit(97)
            if time.monotonic() > t_end:
                print(f"!!! [{label}] 시간 상한 {limit_s:.0f}초 초과: 중단", file=sys.stderr, flush=True)
                os._exit(98)
            time.sleep(0.5)

    th = threading.Thread(target=run, daemon=True, name="microscope-guard")
    th.start()
    return th
