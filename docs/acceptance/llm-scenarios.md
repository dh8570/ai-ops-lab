# LLM 알림 합격 시험 F·G·H와 R4 (A1-T4) 실행 안내

이 문서의 명령은 **사용자가 직접** 실행한다. 아래 모든 명령은 ai-lab-gpu(VM)의 레포 루트(`~/ai-ops-lab`)에서 쓴다.
시험 스크립트는 `scripts/acceptance/llm_alerts.py`, 가짜 Ollama는 `scripts/acceptance/fake_ollama_server.py`이다.
기존 시험 A~E(`scripts/acceptance/alerts.py`)는 바꾸지 않았고, 아래 시험이 그 판정 함수를 그대로 가져다 쓴다.

| 시험 | 증명하는 것 | 가짜 서버 모드 | 예상 소요(추정) |
| --- | --- | --- | --- |
| F | Ollama가 없어도 기본 알림이 간다 | 쓰지 않음(watcher가 닫힌 포트를 부름) | 약 9분 |
| G | LLM이 느려도 감시 루프가 멈추지 않는다 | slow, 90초 지연 | 약 9분 |
| H | 정상일 때 분석이 장애당 한 번, FIRING 뒤에 온다 | analysis | 약 18~21분(장애 2번) |
| R4 | 앱 정지(R4)로 연 장애도 분석이 최대 한 번이고, 로그 창 줄이 빠진다 | analysis | 약 10분 |

소요 시간은 실측이 아니라 `alerts.py`의 안내(에러 주입 10분이 약 15분, 앱 정지 약 9~10분)에서 계산한 추정이다.
F·G·H는 주입 120초 + 복구 판정 대기(`resolve_windows` 5창 = 약 5분) + 탐지 대기로 약 8~9분이다.
준비(빌드, 다시 만들기, 창 대기)까지 합치면 전체가 약 1시간이다. 첫 실행은 실측으로 이 표를 고친다.

## 1. 시작 전에

1. 이 브랜치(`feat/a-accept-llm`)가 병합된 최신 main을 쓴다. 스크립트와 compose 프로필이 거기 있다.
2. 실행 중이어야 하는 것: `app`, `db`, `watcher`, `traffic`(`docker compose --profile traffic up -d traffic`). 읽기만 하는 확인:
   ```
   docker compose ps
   docker compose --profile traffic ps traffic
   ```
3. 시험 스크립트는 시작할 때 watcher의 `start` 이벤트를 읽어 **알림이 dryrun(웹훅 없음)이 아니면 시작하지 않는다**(종료 코드 2).
   실제 Slack으로 시험 알림이 나가지 않게 하는 장치이다. 아래 명령이 `NOTIFY_WEBHOOK_URL=`(빈 값)으로 watcher를 다시 만드는 이유이다.
4. 셸에 `LLM_*`, `WATCHER_OLLAMA_URL`, `NOTIFY_WEBHOOK_URL`이 export되어 있으면 명령 앞의 값이 아니라 그것이 쓰일 수 있다.
   `env | grep -E 'LLM_|WATCHER_|NOTIFY_'`가 비어 있는지 먼저 본다.

## 2. 가짜 Ollama와 시험용 watcher 준비

```
# (1) 가짜 서버. 호스트로 공개하지 않는다(ports 없음). 먼저 띄워야 watcher의 예열 호출이 받는다.
docker compose --profile llmtest up -d fake-ollama
docker compose --profile llmtest logs --tail 3 fake-ollama      # "fake-ollama 시작 0.0.0.0:11434 모드 analysis"

# (2) watcher만 다시 빌드하고 다시 만든다: LLM 켬, 가짜 서버로 연결, 알림은 dryrun
LLM_ENABLED=1 WATCHER_OLLAMA_URL=http://fake-ollama:11434 NOTIFY_WEBHOOK_URL= \
  docker compose up -d --build --no-deps --force-recreate watcher
```
- `--no-deps`: `app`·`db`를 다시 만들지 않는다. `--force-recreate`: 설정이 같아 보여도 새 컨테이너로 만든다.
- 빈 `NOTIFY_WEBHOOK_URL=`은 `.env`의 값보다 앞선다(셸 환경변수가 `.env`를 이긴다). 레포 밖 임시 compose 파일로 확인했다.

```
# (3) 창이 한두 번 처리될 때까지 약 2분 기다린 뒤 확인한다
sleep 130
docker compose logs --no-log-prefix watcher | grep -E '"event": "(start|warmup_done|warmup_failed)"' | tail -3
#   기대: "notify_mode": "dryrun", "llm_enabled": true, 그리고 warmup_done
```

## 3. 시나리오 실행

한 번에 하나씩, 앞 시험이 끝나고 **2~3분 지난 뒤** 시작한다(앞 장애가 복구되어 최근 창이 NORMAL이 되어야 한다).
종료 코드 2와 "사전 조건 불충족"이 나오면 시험이 시작되지 않은 것이니 이유를 읽고 조금 뒤 다시 한다.
각 시나리오는 시작할 때 가짜 서버 모드를 맞추고, 끝나면(중단되어도) `analysis`로 되돌린다.

```
python3 scripts/acceptance/llm_alerts.py --scenario g     # LLM 느림.  약 9분
python3 scripts/acceptance/llm_alerts.py --scenario h     # 정상.      약 18~21분 (장애 2번)
python3 scripts/acceptance/llm_alerts.py --scenario r4    # 앱 정지.   약 10분 (앱을 2분 멈춘다. 끝나면 되살린다)
```

F는 watcher가 **닫힌 포트**를 부르게 다시 만든 뒤 한다(컨테이너 자기 자신의 9번 포트는 닫혀 있어 연결이 거부된다).
```
LLM_ENABLED=1 WATCHER_OLLAMA_URL=http://127.0.0.1:9 NOTIFY_WEBHOOK_URL= \
  docker compose up -d --no-deps --force-recreate watcher
sleep 130
python3 scripts/acceptance/llm_alerts.py --scenario f     # Ollama 없음. 약 9분
```

주입 시간은 기본 120초이고 `--fault-sec`로 바꾼다(바꾸면 소요 시간도 달라진다).
출력은 `alerts.py`와 같은 모양이다: `== 결과 ==`의 A~C(기존 판정) 아래에 `== LLM 결과 ==`와 이벤트 목록이 이어진다.
종료 코드: 0 전부 PASS / 1 FAIL 있음 / 2 사전 조건 불충족·실행 불가 / 130 사용자 중단.

판정 항목:

| 항목 | 합격 조건 |
| --- | --- |
| F (A·B·C) | 기존 판정 그대로. FIRING이 시한(120초) 안에 정확히 1건, RESOLVED 도착 |
| F1~F4 | `llm_failed` reason=`connect_error` 1건 / ANALYSIS_FAILED 알림 1건(FIRING 뒤) / ANALYSIS 알림·`llm_done` 0건 / `worker_error` 0건 |
| G1 | 창 처리(`window`) 사이 최대 간격 ≤ window_sec + 15초 |
| G2 | `llm_failed` reason=`timeout` 1건이 FIRING 25~40초 뒤에 오고 `wall_ms`도 25000~40000 |
| G3·G4 | ANALYSIS_FAILED 알림 1건·ANALYSIS 0건 / `worker_error` 0건 (FIRING 시한은 기존 A·B) |
| H1 | ANALYSIS 알림이 장애당 정확히 1건이고 FIRING 알림 뒤(로그 순서)에 온다 |
| H2 | `llm_done` 1건, `llm_failed` 0건, ANALYSIS_FAILED 알림 0건 |
| H3 | `/data/llm_runs.jsonl`에 그 장애의 기록이 한 줄, status ok, 옵션 7개가 모두 있다 |
| H4 | 두 장애의 prompt 단계 `system_sha256`이 같다(프리픽스 캐시용 고정 글) |
| R4 (A~E) | `alerts.py`의 stop 시나리오가 그대로 판정 |
| S1~S3 | 분석 알림이 최대 1건 / 기록이 한 줄 / 그 기록의 사용자 글에 "[최근"이 없고 "로그 창이 닫히기 전에 열려"가 있다 |

숫자의 근거: G2의 30초는 `LLM_TIMEOUT_SEC` 기본값이다(바꿨다면 이 범위도 같이 바꿔야 한다). 로그 시각이 초 단위라 ±1초,
FIRING 알림 뒤 분석 요청이 나가기까지 몇 초가 더 걸릴 수 있어 25~40초로 잡았다. G1의 +15초는 감시 틱(15초) 하나를 더한 값이다.
LLM 호출이 감시 루프를 막았다면 최소 30초 멈추므로 간격이 90초 이상이 되어 걸러진다(15초 미만의 지연은 이 시험이 못 잡는다).
G의 지연 90초는 30초 상한보다 충분히 커서 타임아웃이 반드시 먼저 일어난다.

## 4. 끝난 뒤 원래대로

```
# (1) 가짜 서버 내리기(이 컨테이너만. down은 쓰지 않는다)
docker compose --profile llmtest stop fake-ollama
docker compose --profile llmtest rm -f fake-ollama

# (2) 운영 설정으로 watcher 다시 만들기: 앞에 환경변수를 붙이지 않는다 → .env와 compose 기본값(LLM_ENABLED 기본 0)
docker compose up -d --no-deps --force-recreate watcher

# (3) 확인: notify_mode가 운영 값(웹훅이 있으면 slack), llm_enabled가 운영 값인지
docker compose logs --no-log-prefix watcher | grep '"event": "start"' | tail -1
```
`.env`에 `LLM_ENABLED=1`이 있다면 (2) 뒤에도 켜진 상태가 맞다. 이미지 `python:3.12-slim`은 지우지 않는다.
주의: 운영 설정으로 되돌린 뒤에 `alerts.py`나 `llm_alerts.py`를 돌리면 웹훅이 있을 때 실제 Slack으로 알림이 나간다.
`llm_alerts.py`는 이를 감지하면 시작하지 않지만 `alerts.py`는 감지하지 않는다(기존 동작).

## 5. 시험 중 하지 말 것

- explorer 사용, 현미경(`scripts/check-microscope.sh` 포함) 실행, 큰 빌드나 모델 시험. 이 VM(8코어)의 CPU를 쓰면 G1의 창 간격과 G2의 시각이 흔들린다.
- `app`, `db`, `traffic`, `watcher`를 시험 도중에 재시작하거나 멈추기. (R4가 앱을 멈췄다가 되살리는 것은 시험 스크립트가 한다.)
- 두 시험을 동시에 실행하기(`alerts.py`와 `llm_alerts.py` 포함). 같은 loadgen 컨테이너 이름을 쓴다.
- 시험 중 Ctrl+C는 가능하다. 가짜 서버 모드 복원과 loadgen·앱 정리는 스크립트가 한다.

## 6. 문제가 생기면

- 가짜 서버 모드를 직접 되돌리기:
  ```
  docker compose --profile llmtest exec -T fake-ollama python -c "import json,urllib.request as u;print(u.urlopen(u.Request('http://127.0.0.1:11434/__mode',json.dumps({'mode':'analysis'}).encode(),{'Content-Type':'application/json'})).read().decode())"
  ```
- 현재 모드와 받은 요청 수: `docker compose --profile llmtest exec -T fake-ollama python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:11434/__mode').read().decode())"`
- watcher가 마지막으로 보낸 프롬프트 확인(장애 요약 숫자만 있고 비밀값은 없다): 같은 방식으로 `/__last`를 부른다.
- 분석 기록 한 줄 보기: `docker compose exec -T watcher tail -n 1 /data/llm_runs.jsonl`
- F가 `llm_done`을 보여 FAIL이면 watcher가 닫힌 포트가 아니라 가짜 서버를 부르는 것이다. 2절이 아니라 3절의 F 명령으로 다시 만들었는지 본다.

## 7. 서버(controller1)에서 하는 수동 확인 (사용자가 서버 접속 가능한 시간에)

실제 Ollama가 멈췄을 때도 기본 알림이 가는지 같은 F 시험으로 확인한다. 이 부분은 Claude가 서버에 접근할 수 없어서 하지 않았고 사용자가 한다.

1. (VM) watcher를 실제 Ollama 주소(compose 기본값)로, 알림은 dryrun으로 다시 만든다. 가짜 서버는 쓰지 않는다.
   ```
   LLM_ENABLED=1 NOTIFY_WEBHOOK_URL= docker compose up -d --no-deps --force-recreate watcher
   ```
2. (controller1) Ollama를 멈춘다. 서비스 이름이 `ollama`인지는 서버에서 `systemctl status ollama`로 먼저 확인한다(아직 확인하지 못했다).
   ```
   sudo systemctl stop ollama
   ```
3. (VM) 약 2분 뒤 F 시험을 그대로 실행한다. 닫힌 포트 대신 실제로 멈춘 Ollama에 연결이 거부된다.
   ```
   sleep 130 && python3 scripts/acceptance/llm_alerts.py --scenario f
   ```
   기대: FIRING 1건, `llm_failed`(connect_error) 1건, ANALYSIS_FAILED 1건, ANALYSIS 0건. 예열 실패(`warmup_failed`)는 정상이다.
   Slack에 실제로 도착하는지까지 보고 싶다면 1번에서 `NOTIFY_WEBHOOK_URL=`을 빼고 직접 한 번 주입한다. 이 경우 **실제 Slack으로 시험 알림이 나가며**
   `llm_alerts.py`는 dryrun이 아니라서 시작하지 않는다.
4. (controller1) 시험이 끝나면 바로 되살린다. 다른 사람이 explorer나 현미경을 쓰고 있다면 그동안 못 쓴다.
   ```
   sudo systemctl start ollama
   ```
   (VM) `curl -s http://10.97.0.1:11434/api/version`으로 응답을 확인하고, 4절의 (2)(3)으로 watcher를 운영 설정으로 되돌린다.
