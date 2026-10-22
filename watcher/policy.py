"""Watcher의 판단 규칙. 루프와 분리해 단위 테스트로 검증한다."""
from state import Incident, age_sec


def should_open_from_probe(was_firing: bool, now_firing: bool, open_incident: Incident | None) -> bool:
    """창 사이에서 R4가 '막' 켜졌고 열린 장애가 없을 때만 즉시 장애를 연다.

    - 이미 켜져 있던 R4(was_firing)로 매 틱 다시 열면 같은 장애에 FIRING이 반복된다.
    - 로그 규칙으로 이미 열린 장애가 있으면 R4는 다음 창 판정에서 그 장애에 합쳐지면 된다.
    """
    return now_firing and not was_firing and open_incident is None


def should_notify_update(action: str, inc: Incident | None, grace_sec: float) -> bool:
    """장애 초반 유예 시간 안에 붙은 신호는 같은 원인의 후속 신호로 보고 알리지 않는다.

    프로브(R4)로 연 장애에 75초 뒤 로그 규칙이 붙자 UPDATE가 바로 나간 사례가 있었다.
    같은 원인이 시간차로 드러난 것이므로, 유예 시간이 지난 뒤 추가된 신호만 '원인 변화'로 알린다.
    """
    return action == "escalate" and inc is not None and age_sec(inc) > grace_sec


def should_update_baseline(action: str, requests: int, min_requests: int) -> bool:
    """열린 장애가 없는 NORMAL 창이고 표본이 충분할 때만 기준선에 반영한다.

    - 빈 창(0건)이 들어가면 중앙값이 0으로 끌려 내려가 R3가 무력화됐다.
    - 장애 중·회복 중 창(대기열 해소 폭증 포함)이 들어가면 기준선이 부풀어 오탐이 났다.
    """
    return action == "none" and requests >= min_requests
