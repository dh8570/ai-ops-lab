"""시험 공통 설정: 스레드 수 고정, 메모리 감시(MemAvailable < 8GiB면 중단), 전체 40분 상한."""
import torch

from microscope.engine import config, guard

TIME_LIMIT_S = 40 * 60


def pytest_sessionstart(session):
    torch.set_num_threads(config.threads())
    torch.set_grad_enabled(False)
    guard.start_guard(TIME_LIMIT_S, label="pytest")
