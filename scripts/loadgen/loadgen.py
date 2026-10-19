"""visit-counter 부하·장애 주입기 (개방형 부하 모델)

서버 응답과 무관하게 정해진 속도로 요청을 보내, 실제 사용자 트래픽처럼 과부하를 재현한다.

예시:
  docker compose run --rm loadgen --rps 5 --duration 60
  docker compose run --rm loadgen --rps 5 --error-rate 0.4 --duration 60
  docker compose run --rm loadgen --rps 5 --slow-rate 0.3 --slow-delay 3 --duration 60
"""
import argparse
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

p = argparse.ArgumentParser(description="visit-counter 부하·장애 주입기")
p.add_argument("--target", default="http://app:8000")
p.add_argument("--rps", type=float, default=5, help="초당 요청 수")
p.add_argument("--duration", type=int, default=60, help="실행 시간(초)")
p.add_argument("--error-rate", type=float, default=0.0, help="/error 비율 (0~1)")
p.add_argument("--slow-rate", type=float, default=0.0, help="/slow 비율 (0~1)")
p.add_argument("--slow-delay", type=float, default=3.0, help="/slow 지연(초)")
p.add_argument("--timeout", type=float, default=15, help="클라이언트 타임아웃(초)")
args = p.parse_args()

if args.rps <= 0:
    p.error("--rps는 0보다 커야 합니다")
if not 0 <= args.error_rate + args.slow_rate <= 1:
    p.error("--error-rate와 --slow-rate의 합은 0~1 사이여야 합니다")

stats = Counter()
latencies = []
lock = threading.Lock()


def pick_path() -> str:
    r = random.random()
    if r < args.error_rate:
        return "/error"
    if r < args.error_rate + args.slow_rate:
        return f"/slow?delay={args.slow_delay}"
    return "/"


def hit(path: str) -> None:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(args.target + path, timeout=args.timeout) as res:
            code = res.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (TimeoutError, socket.timeout):
        code = "timeout"                    # 응답 대기 중 시간 초과
    except urllib.error.URLError as e:
        r = e.reason
        if isinstance(r, (TimeoutError, socket.timeout)):
            code = "timeout"                # 연결 수립 중 시간 초과
        elif isinstance(r, ConnectionRefusedError):
            code = "refused"                # 받아줄 프로세스 없음
        else:
            code = f"urlerror:{type(r).__name__}"
    except ConnectionResetError:
        code = "reset"                      # 서버가 연결을 끊음
    except Exception as e:
        code = f"other:{type(e).__name__}"
    with lock:
        stats[code] += 1
        latencies.append((time.perf_counter() - t0) * 1000)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


threads = []
interval = 1 / args.rps
start = time.monotonic()
end = start + args.duration
next_send = start
next_report = start + 10

while (now := time.monotonic()) < end:
    if now >= next_send:
        t = threading.Thread(target=hit, args=(pick_path(),), daemon=True)  # 응답을 기다리지 않음
        t.start()
        threads.append(t)
        next_send += interval           # 예정 시각 기준: 지연이 생겨도 누적되지 않음
    if now >= next_report:
        with lock:
            print(f"[10s] {dict(stats)}", flush=True)
        next_report += 10
    time.sleep(max(0, min(next_send, next_report) - time.monotonic()))

deadline = time.monotonic() + args.timeout + 5   # 전체 마감 시각 하나만 사용
for t in threads:
    t.join(timeout=max(0, deadline - time.monotonic()))

with lock:
    done = sum(stats.values())
    print(f"[final] sent={len(threads)} done={done} unfinished={len(threads) - done} {dict(stats)} "
          f"client_p50={pct(latencies, .5):.0f}ms client_p95={pct(latencies, .95):.0f}ms", flush=True)