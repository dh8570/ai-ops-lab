import socket
import time
import urllib.error
import urllib.request


class Prober:
    """사용자와 같은 경로로 /healthz를 호출해 응답 가능 여부를 잰다 (합성 프로브)."""

    def __init__(self, url: str, timeout: float, fail_threshold: int):
        self.url, self.timeout, self.fail_threshold = url, timeout, fail_threshold
        self.consecutive_failures = 0
        self.last_detail = "아직 실행 전"
        self._failing_in_window = False

    @property
    def firing(self) -> bool:
        return self.consecutive_failures >= self.fail_threshold

    def probe(self) -> dict:
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as res:
                ok, detail = res.status == 200, f"HTTP {res.status}"
        except urllib.error.HTTPError as e:
            ok, detail = False, f"HTTP {e.code}"
        except TimeoutError:
            ok, detail = False, "timeout"
        except urllib.error.URLError as e:
            r = e.reason
            ok, detail = False, "timeout" if isinstance(r, TimeoutError | socket.timeout) else type(r).__name__
        except Exception as e:
            ok, detail = False, type(e).__name__

        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
        self.last_detail = detail
        if self.firing:
            self._failing_in_window = True        # 창 도중 한 번이라도 켜졌는지 기억
        return {"ok": ok, "detail": detail, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "consecutive_failures": self.consecutive_failures}

    def take_window_flag(self) -> bool:
        """이번 창 동안 R4가 켜진 적이 있는지 돌려주고 초기화한다."""
        flag = self._failing_in_window or self.firing
        self._failing_in_window = False
        return flag
