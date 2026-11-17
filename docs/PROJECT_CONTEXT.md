# 프로젝트 인수인계 문서 (PROJECT_CONTEXT)

최종 갱신: 2026-11-17 · 작성 배경: claude.ai 대화에서 진행하던 프로젝트를 Claude Code로 이어가기 위한 맥락 정리

---

## 1. 목표와 진행 방식

- 목적: "LLM 기반 장애 분석 어시스턴트"를 만들며 어떤 문제를 AI로 푸는지, AI가 어떻게 동작하는지, 데이터를 어디에 저장해 어떻게 쓰는지를 배운다. 4주(2026-10-27 ~ 11-27, 11-17에 일정 재조정). 비용 0원 원칙.
- 원칙: AI 우선(인프라는 AI 단계를 막을 때만 최소로), 매주 "AI 질문 세 가지"와 채점표·비교표 1개 이상, 모르는 것은 질문하며 배운다(용어는 docs/glossary.md).
- 계획과 주별 상세는 `docs/plan/ai-first-4weeks.md`가 유일한 원본이다.
- (구버전) 24주 로드맵(워킹 스켈레톤 후 ELK·Ansible·Prometheus·Airflow·Terraform·OCI 등으로 교체)은 2026-10-27에 4주 AI 우선 계획으로 대체했다.
- 설계 그림: `docs/figures/` (Fig 2는 v0.3 2026-10-27, Fig 5는 v0.6 2026-11-17. Fig 1·3·4는 v0.2. Fig 3·4는 A주 설계 결정 뒤에 고친다).

## 2. 환경

| 구성 | 내용 |
| --- | --- |
| 물리 서버 controller1 | Ubuntu 26.04.1, 논리 CPU 48개, 메모리 251GB, 디스크 1.8TB, GPU RTX 4080 16GB(드라이버 595-open). 기존 업무 서비스는 꺼 둠. 서버에서의 명령 실행은 사용자가 직접 한다 |
| VM ai-lab-gpu (Multipass) | Ubuntu 24.04, 8코어 / 32GB / 100GB. 모든 실습 공간. 사용자 `ubuntu`. Multipass 1.17.0(업데이트 보류), 스냅샷 `docker-ready`. GPU는 서버에만 있고 VM에는 없다 |
| VM↔서버 통로 | 10.97.0.0/24, 서버 쪽 10.97.0.1. Ollama는 서버에서 돌고 이 주소(10.97.0.1:11434)에만 공개 중(2026-10-27 설치, 0.40.2, 모델 qwen2.5:7b Q4_K_M, systemd 서비스) |
| 접속 | Tailscale + **표준 OpenSSH**. Tailscale은 `--ssh` 없이 쓴다(Tailscale SSH는 끔, 2주차 결정) |
| 개발 도구 | Windows의 VS Code / Cursor Remote-SSH, Claude Code |
| 알림 | Slack `#alerts` (Incoming Webhook, `.env`의 `NOTIFY_WEBHOOK_URL`) |
| 비상 접속 | 서버에서 `multipass shell ai-lab-gpu` |
| 옛 환경 | jn-controller1 위 VM ai-lab (서비스 중지, 보관). 옛 CPU 측정 결과 압축본은 옛 VM 홈의 `llm-latency-out-20261026.tgz`, 측정 스크립트 초안은 `feat/3-llm` 브랜치(wip) |

## 3. 레포와 서비스 구성

```
ai-ops-lab/
├── app/                 Flask + gunicorn(sync 워커 2) 방문 카운터
├── watcher/             감지·상태 관리·알림 (표준 라이브러리만)
├── scripts/loadgen/     개방형 부하·장애 주입기 (loadgen·traffic 서비스가 공유)
├── docs/figures/        설계 그림
├── docs/figures/src/    설계 그림 생성 소스 (python3 docs/figures/src/build.py로 5장 재생성)
├── docs/retros/         주차 회고(YYYY-wNN-retro.md, 노션에도 복사본 보관)와 조사 보고서
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
- 2주차 완료 (2-A, 2-B, 2-C, PR #3 병합. `feat/2-watcher` 브랜치)
  - 2-A 완료: 로그 집계, R1~R3, 중앙값 기준선, 배경 트래픽
  - 2-B 완료: 상태 머신, SQLite, Slack 알림, SIGTERM, 경과 시간 보정, app `count(*)` 제거
  - 2-C 완료: R4 프로브, UPDATE 알림(180초 유예), policy 분리, 단위 테스트 29개, ruff·pytest 검사 스크립트, 알림 합격 시험 자동화(errors·stop 시나리오)
- 새 서버(controller1 위 ai-lab-gpu)로 이전 완료 (합격 시험 통과)
- 알림 실패 원인 기록 보강 (PR #5 병합)
- 탐험 사이트 S0 병합 (PR #7)
- 현미경 엔진 스파이크 통과: Qwen2.5-7B bf16을 VM CPU에서 실행, Prefill 9.3초(72토큰), Decode 0.66초/토큰, 메모리 14.7GiB
- 현미경 엔진 M2-a·M2-b: Prefill·Decode가 HF eager와 비트 단위로 일치(PR #12 병합)
- AI 분석 알림(T1~T4, PR #10·#11·#13·#14), 채점표와 프롬프트 비교(docs/eval, PR #16~#18), 프롬프트 스위치와 그림자 모드(PR #19·#20)가 병합되었다. 시험 489개.
- **현재: 2026-11-17 일정 재조정(마감 11/27). A주 완료, B주 프롬프트 스위치·그림자 모드 병합(PR #19·#20). 진행: 공개 내보내기(안전 모듈과 장애 기록 화면)**

## 5. 다음 할 일

**세부는 `docs/plan/ai-first-4weeks.md`(2026-11-17 재조정, 마감 11/27).**

- 11/18~11/19: 공개 링크 1차(내보내기 조립과 시험, gh-pages 배포)
- 11/20~11/22: RAG 최소판 비교, 저장 데이터 설명, 규칙 대 ML 최소 비교, 11/22 범위 동결
- 11/23~11/27: 채점표·비교표 모음, 공개 링크 2차, 최종 문서, 시연, 11/26 버퍼
- 운영에서 AI 분석을 켜는 결정은 그림자 모드 기록과 사람 채점이 안정된 뒤(지금 운영 채널에는 분석이 가지 않는다)

### 5-1. 알려진 과제

- Docker 컨테이너 로그(json-file)와 app.jsonl이 회전 없이 증가 → 2-1에서 처리
- 아이폰 Termius는 OpenSSH 키 등록 필요
- 느린 성능 저하 시나리오는 2-4에서 켜고 끌 수 있는 장애 주입으로 구현
- 포화 중에는 다른 에러 신호가 대기열에 묶였다가 해소 시 희석됨(R1 미탐) → 런북과 LLM 맥락에 반영
- 남은 빚은 docs/retros/2026-w02-retro.md의 '아직 남은 것' 절 참고
- Slack 알림 간헐 실패: 원인 미확인. 재발하면 `notify_failed`의 attempts 기록으로 조사
- app 컨테이너와 이미지 이름표 불일치: 내용은 같을 것으로 추정. 다음 재생성 때 합격 시험으로 확인
- 설계 그림 Fig 2에는 explorer(탐험 사이트)·microscope(현미경 엔진) 서비스가 없다. D주 그림 정리에서 추가한다.

## 6. 기록 형식 (노션)

### 6-1. 표 형식

- 트러블슈팅: `| 날짜 | 분류 | 증상 | 원인 | 해결 | 해결 여부 |`
- 의사결정: `| 날짜 | Phase | 결정 | 이유 | 대안 |`
- 학습 메모: `| 제목 | 날짜 | 단계 | 분야 | 계기 | 내용 | 개념 노트 반영 |` (분야: Docker, Linux, 네트워크, 통계, 성능, 설계, DB, 알림, 보안, Python, 진단, 운영 / 계기: 트러블슈팅, 실험, 질문, 코드 리뷰)
- 학습 노트(개념 노트): 한 줄 요약 → 쉬운 설명 → 핵심 용어 표 → 심화 설명 → 이 프로젝트에서의 역할 → 실행한 명령어와 결과 → 트러블슈팅·의사결정 → 자주 쓰는 명령어 → 셀프 체크
- 포스트모템: 요약 → 영향 → 원인 → 탐지 → 해결 → 교훈
- 표는 항상 최신 항목이 맨 위. 같은 날짜 안에서는 나중에 일어난 일이 위.
- 매주 끝날 때 docs/retros/YYYY-wNN-retro.md로 회고를 남기고 노션에도 복사한다. 원본은 레포 파일이고, 고칠 때는 레포를 먼저 고친 뒤 노션에 다시 붙인다. 형식은 w02 회고를 따른다.

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
- stop 시험은 앱을 Watcher 로그 창 직후에 멈춰 틱 위상을 고정하고, 경로 통과(E)를 판정에 포함한다

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
- 시험이 PASS했다는 것과 목표 경로를 지났다는 것은 별개다(경로 커버리지)
- 타이밍에 의존하는 경로는 시험이 위상을 제어해 결정적으로 만든다
