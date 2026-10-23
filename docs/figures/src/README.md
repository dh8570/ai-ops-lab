# 설계 그림 소스

`docs/figures/`의 PNG 5장(Fig 1~5)을 만드는 소스다. 그림도 코드처럼 변경 이력을 남기고 리뷰하기 위해 레포에서 관리한다.

## 다시 만들기

```bash
# 최초 1회: 폰트와 라이브러리 (Ubuntu)
sudo apt-get install -y fonts-noto-cjk fonts-dejavu-core libcairo2
python3 -m venv ~/.venvs/figures && . ~/.venvs/figures/bin/activate
pip install -r docs/figures/src/requirements.txt

# 그림 5장 전체 재생성 (레포 루트에서)
python3 docs/figures/src/build.py
```

## 구성

| 파일 | 내용 |
| --- | --- |
| `lib.py` | 공통 그리기 도구(색, 도형, 화살표, 머리말·범례), `VERSION`, PNG 변환 |
| `fig1.py` | 개요: 문제·접근, 목표 지표, 범위, 알림 예시 |
| `fig2.py` | 시스템 아키텍처 (호스트 · VM · Compose 경계) |
| `fig3.py` | 장애 감지 → 알림 시퀀스 (15초 틱, 프로브, UPDATE) |
| `fig4.py` | 장애 상태 모델과 탐지 규칙(R1~R4)·운영 파라미터 |
| `fig5.py` | 교체 로드맵(간트)과 v0 완료 기준 |

## 수정할 때 지킬 것

- 설계가 바뀌면 `docs/PROJECT_CONTEXT.md`의 설계 결정과 **그림을 같은 PR에서** 함께 고친다.
- `lib.py`의 `VERSION`을 올린다. (예: v0.2 · 2026-10-22 → v0.3 · 날짜)
- 한글이 들어간 줄은 고정폭 글꼴(`mono=True`)로 그리지 않는다. 고정폭 글꼴에는 한글 글리프가 없어 글자가 깨진다.
- 수정 후 PNG를 눈으로 확인한다. 글자가 상자 밖으로 넘치거나 화살표 라벨이 겹치지 않는지 본다.
