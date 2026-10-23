# 프로젝트 인수인계 문서 (PROJECT_CONTEXT)

최종 갱신: 2026-10-22 · 작성 배경: claude.ai 대화에서 진행하던 프로젝트를 Claude Code로 이어가기 위한 맥락 정리

---

## 1. 목표와 진행 방식

- 목표: AI·AX, AIOps, 인프라를 엮은 "LLM 기반 장애 분석 어시스턴트"를 만들고, 그 과정을 실무 수준으로 설명할 수 있게 되는 것. 비용 0원 원칙.
- 방식: **워킹 스켈레톤**. 1~3주차에 가장 단순한 방법으로 전체 흐름(앱 → 로그 → 감지 → LLM 요약 → 알림)을 끝까지 연결하고, 4~21주차에 부품을 하나씩 실무 기술로 교체(ELK, Ansible, Prometheus, Isolation Forest, RAG, Airflow, 에이전트, Terraform·OCI). 22~24주차 시연·포트폴리오.
- 설계 그림: `docs/figures/` (Fig 1~4는 v0.1, Fig 5는 v0). 2주차 결정 일부가 아직 그림에 반영되지 않음(아래 "남은 일" 참고).

## 2. 환경

| 구성 | 내용 |
| --- | --- |
| 물리 서버 jn-controller1 | Ubuntu 26.04, 업무 서비스 컨테이너 공존. VM 관리만 한다. 코드·키를 두지 않는다 |
| VM ai-lab (Multipass) | Ubuntu 24.04, 8 vCPU / 32GB / 150GB. 모든 실습 공간. 사용자 `ubuntu` |
| 접속 | Tailscale(tailnet) + **표준 OpenSSH**. ai-lab Tailscale IP `100.84.255.38`. Tailscale SSH는 끔(2주차 결정) |
| 개발 도구 | Windows의 VS Code / Cursor Remote-SSH, Claude Code |
| 알림 | Slack `#alerts` (Incoming Webhook, `.env`의 `NOTIFY_WEBHOOK_URL`) |
| 비상 접속 | 물리 서버에서 `multipass shell ai-lab` |

## 3. 레포와 서비스 구성

```
ai-ops-lab/
├── app/                 Flask + gunicorn(sync 워커 2) 방문 카운터
├── watcher/             감지·상태 관리·알림 (표준 라이브러리만)
├── scripts/loadgen/     개방형 부하·장애 주입기 (loadgen·traffic 서비스가 공유)
├── docs/figures/        설계 그림
├── docs/retros/         조사 보고서 (2026-10-19 slow 시나리오 goodput 붕괴)
├── docker-compose.yml   name: ai-ops-lab
└── .env / .env.example  POSTGRES_*, APP_BIND_IP, NOTIFY_PROVIDER, NOTIFY_WEBHOOK_URL
```

Compose 서비스:

| 서비스 | 역할 | 비고 |
| --- | --- | --- |
| db | postgres:16 | 볼륨 db-data, 외부 포트 없음 |
| app | Flask 방문 카운터 | `APP_BIND_IP:8000`(Tailscale IP에만 바인딩), 로그 볼륨 app-logs |
| watcher | 감지·알림 | app-logs 읽기 전용, 상태 볼륨 watcher-state(SQLite) |
| traffic | 상시 배경 트래픽 5 rps | profile `traffic`, 1시간마다 재시작, `init: true` |
| loadgen | 장애 주입용 | profile `load`, `docker compose run --rm loadgen <옵션>` |

### app

- 엔드포인트: `/`(방문 기록, `INSERT ... RETURNING id`로 방문 수), `/error`(500), `/slow?delay=N`, `/healthz`(liveness, DB 미확인).
- 로그: `/var/log/app/app.jsonl`에 JSON Lines. 필드 `ts`(UTC ISO), `service`, `request_id`, `method`, `path`, `status`, `latency_ms`, `error`. stdout에도 같은 내용 출력.
- DB 접속은 libpq 표준 `PG*` 환경 변수. HTTPException(404 등)은 500으로 바꾸지 않는다.
- 처리 용량: sync 워커 2개. `/slow` 30%·3초 섞이면 약 2.2 req/s로 떨어져 포화. **의도적으로 유지**(포화 장애 시나리오).

### loadgen

옵션: `--rps`, `--duration`, `--error-rate`, `--slow-rate`, `--slow-delay`, `--timeout`(기본 15). 개방형 부하(예정 시각 기준 전송), 실패 유형 분류(timeout/refused/reset), `[final] sent/done/unfinished` 및 client p50/p95 출력.

자주 쓰는 시나리오(배경 트래픽 위에 덧붙임):

```bash
docker compose run --rm loadgen --rps 3 --error-rate 1.0 --duration 120                 # R1
docker compose run --rm loadgen --rps 1.5 --slow-rate 1.0 --slow-delay 3 --duration 180  # R2+R3+R4 포화
docker compose run --rm loadgen --rps 4 --slow-rate 1.0 --slow-delay 1.5 --duration 180  # R3 단독(R2 미탐)
docker compose stop app; (2분 뒤) docker compose start app                                # 완전 정지(R4, R3)
```

**시나리오마다 RESOLVED를 확인한 뒤 다음으로 넘어간다.** RECOVERING 중에 다음 장애를 넣으면 같은 장애로 합쳐진다.

### watcher (모듈과 동작)

| 모듈 | 역할 |
| --- | --- |
| config.py | 환경 변수 기반 설정 (기본값이 곧 Fig 4의 기준값) |
| logreader.py | offset 기반 읽기, 미완성 줄 보관, 잘림·교체 감지, snapshot/restore |
| stats.py | 창 집계(requests, 5xx, p95 nearest-rank, rpm = 건수×60÷실제 경과초), `/healthz` 제외 |
| rules.py | R1~R3 판정 `evaluate`, R4 `probe_finding` |
| probe.py | `/healthz` 합성 프로브, 연속 실패 수, 창 단위 플래그 |
| state.py | SQLite 저장소, `IncidentManager.step` 상태 전이, `age_sec` |
| notifier.py | 플랫폼 무관 Message → Slack(Block Kit)/Discord 변환, dry-run, 재시도, 429 처리 |
| main.py | 15초 틱 루프. 매 틱 프로브, 4번째 틱마다 60초 로그 창 처리. SIGTERM 정상 종료 |

탐지 규칙(초기값):

| 규칙 | 조건 | 심각도 |
| --- | --- | --- |
| R1 | 요청 ≥ 20 그리고 5xx 비율 > 20% | critical |
| R2 | 요청 ≥ 20 그리고 p95 > 2,000ms | warning |
| R3 | 분당 처리 건수 < 기준선 × **0.7**, 기준선 ≥ 60 rpm | warning |
| R4 | `/healthz` 2초 타임아웃 **2회 연속**, 15초 주기 | critical |

핵심 설계:

- **기준선**: 직전 정상 창 rpm의 **중앙값**, 최소 3개 창부터 최대 10개. **열린 장애가 없는 NORMAL 창이고 요청 20건 이상일 때만** 기록(`action == "none"`). 판정 대상 창은 기준선 계산에서 제외.
- **상태 머신**: NORMAL → FIRING → RECOVERING → (5개 창 연속 조용) RESOLVED. RECOVERING 중 재발은 같은 장애(`refire`).
- **그룹핑**: 장애 단위는 `service`. 켜진 규칙은 장애의 증거로 누적(Fig 4의 `service + rule_id` 표기는 구버전).
- **알림**: `fire` → FIRING, `escalate`(새 규칙 추가·심각도 상승) → 장애가 열린 지 **180초 지난 경우만** UPDATE, `resolve` → RESOLVED.
- **R4 즉시 장애**: 창 사이 틱에서 R4가 막 켜졌고 열린 장애가 없으면 창을 기다리지 않고 바로 장애를 연다. 창 처리 때는 "그 창 동안 한 번이라도 R4가 켜졌는가"로 판정.
- **체크포인트**: 읽기 위치·기준선 기록·마지막 읽은 시각을 `meta.checkpoint` 한 행에 원자적으로 저장. 재시작 시 복원.
- **로그**: Watcher 자신도 JSON 한 줄(`start`, `window`, `probe`, `incident`, `notified`, `notify_failed`, `notify_dryrun`, `stop`). 프로브는 실패·상태 변화 때만 기록. `window`에 판단 근거(`baseline_n`, `elapsed_sec`, `added_rules`) 포함.

## 4. 진행 상황

- Phase 0 완료 (환경, 레포, VM, 스냅샷 `docker-ready`)
- 1주차 완료 (app, loadgen, goodput 붕괴 조사, PR 병합)
- 2주차 (`feat/2-watcher` 브랜치, PR은 2-C 끝나고 한 번에)
  - 2-A 완료: 로그 집계, R1~R3, 중앙값 기준선, 배경 트래픽
  - 2-B 완료: 상태 머신, SQLite, Slack 알림, SIGTERM, 경과 시간 보정, app `count(*)` 제거
  - **2-C 진행 중**: R4 프로브, UPDATE 알림(180초 유예), policy 분리, 단위 테스트 29개, ruff·pytest 검사 스크립트 완료. 남은 것은 5-2.

## 5. 다음 할 일

### 5-1. 2-C: 단위 테스트와 린터 (완료)

동작을 바꾸지 않고 현재 동작을 테스트로 고정한다.

1. `watcher/policy.py` 신설, main.py 루프의 조건 3개를 함수로 대체 (의미 동일)
   - `should_open_from_probe(was_firing, now_firing, open_incident)` = `now_firing and not was_firing and open_incident is None`
   - `should_notify_update(action, inc, grace_sec)` = `action == "escalate" and inc is not None and age_sec(inc) > grace_sec`
   - `should_update_baseline(action, requests, min_requests)` = `action == "none" and requests >= min_requests`
2. `watcher/requirements-dev.txt`(pytest==8.3.3, ruff==0.6.9), `watcher/pyproject.toml`(ruff line-length 120, py312, select E W F I B UP / pytest testpaths tests)
3. `scripts/check-watcher.sh`: python:3.12-slim 일회용 컨테이너에서 `ruff check --no-cache .` && `pytest -q -p no:cacheprovider`, 인자 `fix`면 `ruff check --no-cache --fix .`. `-e PYTHONDONTWRITEBYTECODE=1`(root 소유 캐시 파일 방지)
4. `.gitignore`에 `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`
5. `watcher/tests/` (conftest.py에서 watcher/를 sys.path에 추가, 네트워크·Docker 미사용, tmp_path와 monkeypatch 사용)
   - test_rules: 113/300 에러 → R1만 / 요청 10건이면 아무것도 안 켜짐 / p95 3000.7 → R2 / 기준선 None → R3 보류("baseline 수집 중") / rpm 175 vs 300 → R3 / rpm 270 vs 300 → R3 아님 / 기준선 50 → R3 아님
   - test_stats: percentile([1..10], .95) == 10, 빈 목록 0.0 / `/healthz` 제외 / 501건 ÷ 100.1초 → rpm 300.3
   - test_logreader: 기존 줄 건너뜀 / 미완성 줄 보관 후 완성 / 잘림 시 처음부터 / 깨진 JSON은 bad로 셈 / snapshot→줄 추가→restore 시 그 줄을 읽음
   - test_state: R1 두 번 후 빈 판정 6번 → fire, suppress, recovering×4, resolve, none / R2·R3 동시 → 장애 1건 / RECOVERING 중 재발 → refire, 같은 id / R4 장애에 R2 추가 → escalate, added [R2] / 같은 DB로 Store 재생성 시 suppress / 75초 전 장애 → UPDATE 안 함, 1140초 전 → 함 / should_open_from_probe 3케이스 / should_update_baseline 3케이스
   - test_probe_notifier: 타임아웃·타임아웃·성공 순서의 firing 변화 / take_window_flag 기억과 초기화 / UPDATE 메시지 첫 필드와 timestamp ≠ opened_at / Slack payload의 text가 [FIRING]으로 시작, header 블록 없음
6. ruff 지적 수정(자동 → 수동), pytest 전부 통과, `docker compose up -d --build watcher` 후 start 로그 확인

### 5-2. 2주차 마무리 (지금 할 일)

- (완료) v0 합격 시험 중 알림 항목 자동화 스크립트: `scripts/acceptance/alerts.py`, errors 시나리오 기본 모드 약 15분 (2분 내 FIRING, 중복 0, 5분 후 RESOLVED)
- (완료) 앱 정지 시나리오 추가: `--scenario stop`으로 R4 프로브 경로 시험 (앱 120초 정지, FIRING 60초 이내, 중복 0, 복구 후 RESOLVED)
- 설계 그림 갱신: Fig 3(프로브 15초 별도 실행, UPDATE), Fig 4(R3 0.7·중앙값, dedup key = service, UPDATE 유예 180초), Fig 1(UPDATE 알림)
- 2-C 노션 기록 정리 (아래 6-2 목록)
- PR 작성·병합, 2주차 학습 노트("Watcher와 탐지 규칙")

### 5-3. 3주차 예고

Ollama 추가(8B 이하), LLM 호출 전 마스킹·최근 50줄 제한, 런북 2~3개(1주차 에러 시그니처 표, 포화 시 다른 에러가 가려지는 현상 포함), 타임아웃 30초·실패 시 기본 템플릿.

### 5-4. 알려진 과제

- Docker 컨테이너 로그(json-file)와 app.jsonl이 회전 없이 증가 → 2-1에서 처리
- 아이폰 Termius는 OpenSSH 키 등록 필요
- 느린 성능 저하 시나리오는 2-4에서 켜고 끌 수 있는 장애 주입으로 구현
- 포화 중에는 다른 에러 신호가 대기열에 묶였다가 해소 시 희석됨(R1 미탐) → 런북과 LLM 맥락에 반영

## 6. 기록 형식 (노션)

### 6-1. 표 형식

- 트러블슈팅: `| 날짜 | 분류 | 증상 | 원인 | 해결 | 해결 여부 |`
- 의사결정: `| 날짜 | Phase | 결정 | 이유 | 대안 |`
- 학습 메모: `| 제목 | 날짜 | 단계 | 분야 | 계기 | 내용 | 개념 노트 반영 |` (분야: Docker, Linux, 네트워크, 통계, 성능, 설계, DB, 알림, 보안, Python, 진단, 운영 / 계기: 트러블슈팅, 실험, 질문, 코드 리뷰)
- 학습 노트(개념 노트): 한 줄 요약 → 쉬운 설명 → 핵심 용어 표 → 심화 설명 → 이 프로젝트에서의 역할 → 실행한 명령어와 결과 → 트러블슈팅·의사결정 → 자주 쓰는 명령어 → 셀프 체크
- 포스트모템: 요약 → 영향 → 원인 → 탐지 → 해결 → 교훈

### 6-2. 2-C에서 아직 노션에 정리하지 않은 내용

트러블슈팅:
- VS Code·Cursor 원격이 매우 느림, Claude Code 명령 not found → 원인: Tailscale SSH가 VS Code의 동적 포트 전달(`ssh -D`) 통로를 유지하지 못해 20초마다 재연결, 원격 확장 호스트 미기동 → 해결: Tailscale SSH 끄고 표준 OpenSSH + 키 인증, ServerAliveInterval 30
- 진단 중 오판 2건: (1) `ssh ubuntu@ai-lab`의 2.7초는 이름 해석 실패였고 VS Code는 IP를 써서 무관 (2) `ssh ... exit` 0.12초만 보고 SSH가 빠르다고 판단했으나 포트 전달은 측정하지 않았음
- Cursor 원격 서버 잔재(세션 25개, 서버 2개 버전) → 프로세스 종료, `~/.cursor-server` 삭제
- state.py에서 `age_sec`가 `Incident`보다 먼저 정의되어 NameError (실행 전 발견)
- UPDATE 유예를 창 수로 세서 프로브로 열린 장애에 75초 뒤 R3 추가 시 UPDATE 발송 → 시간 기준(180초)으로 변경
- UPDATE 메시지 시각이 장애 시작 시각으로 표시 → 보낸 시각으로 변경

의사결정:
- R4는 스레드 대신 15초 틱 단일 루프 (SQLite·상태 머신 동시 접근 회피)
- R4 창 판정은 "창 동안 한 번이라도 켜졌는가"
- 새 규칙 추가·심각도 상승 시 UPDATE 알림, 장애 초반 180초 유예
- 원격 접속을 Tailscale SSH에서 표준 OpenSSH + 키 인증으로 변경 (Phase 0 결정 대체)
- 판단 규칙을 policy.py로 분리해 단위 테스트, ruff·pytest는 일회용 컨테이너에서 실행(운영 이미지 미포함)
- Claude Code 프로젝트 규칙을 CLAUDE.md에 두고, `.env` 미열람을 명시
- 알림 판정은 채널과 무관하게 notified/notify_dryrun 이벤트의 장애 id별 집계로 한다
- 회귀 게이트는 운영과 같은 조건(실발송)으로 돌린다. 알림 소음은 테스트 채널 분리로 해결 예정

학습 메모 후보:
- 합성 프로브는 앱 로그가 못 보는 대기열·완전 정지를 사용자 경로로 측정한다
- 한 장애가 다른 장애를 가린다(포화 중 에러가 대기열에 묶였다가 희석)
- 6분 과부하에 14분 회복 (회복 시간 ≈ 대기열 ÷ 남는 여력)
- 정의 순서와 타입 표기 평가 시점 (Python 3.12는 함수 정의 시 어노테이션 평가)
- 동시성 문제는 잠금보다 흐름을 하나로 두어 원천 회피할 수 있다
- 측정은 실제 사용 경로를 재야 한다 (명령 실행 vs 포트 전달)
- Ubuntu 24.04의 ssh.socket 활성화, `/run/user/UID`는 정식 로그인 세션의 표지
- 목(mock)과 회귀 테스트, 테스트 가능한 설계(결정과 실행의 분리)
- 에러 주입은 앱이 살아 있어 R4를 거치지 않는다. R4 경로 시험은 앱 정지 시나리오가 필요
