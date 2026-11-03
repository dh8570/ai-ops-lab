# 실행 기록 형식 (schema_version 1)

## 목적

파이프라인 탐험 사이트는 **실행 기록 파일을 읽어 보여 주는 뷰어**다. 실행 하나가 끝나면 JSON 파일 하나가 남고, 화면은 그 파일만 읽는다.
이 기록은 나중에 공개 링크로 내보낼 **재생 가능한 실행 기록**의 바탕이다. 같은 화면 코드가 실행 서버 옆(live)에서도, 파일만 있는 정적 호스팅(static)에서도 같은 기록을 같은 모양으로 보여 준다.

- 화면이 읽는 것: `./data/index.json`, `./data/runs/<run_id>.json`, `./content/*.json` (모두 상대 경로)
- 화면이 서버를 부르는 것: 실행(`./api/run`)과 토큰화 같은 "만드는" 기능뿐이다(live에서만).
- 구현: [explorer/runs.py](../../explorer/runs.py) (기록 만들기·검증·쓰기), [explorer/app.py](../../explorer/app.py) (읽기 경로)

## 파일 위치와 run_id

| 파일 | 위치(컨테이너) | 내용 |
| --- | --- | --- |
| 실행 기록 | `/data/runs/<run_id>.json` | 실행 하나 |
| 목록 | `/data/index.json` | 최신순 최대 200개의 요약 |

- `/data`는 볼륨 `explorer-traces`이고 환경 변수 `DATA_DIR`로 정한다. 옛 `/data/traces.jsonl`은 더 이상 쓰지도 읽지도 않는다(지우지 않았다).
- `run_id` = `r-` + UTC 시각(`YYYYMMDD-HHMMSS`) + `-` + 16진수 4자(`secrets.token_hex(2)`). 예: `r-20261028-085302-d678`.
  같은 이름의 파일이 이미 있으면 다시 만든다.
- 정규식 `^r-\d{8}-\d{6}-[0-9a-f]{4}$`만 허용한다. 서버(`runs.RUN_ID_RE`)와 화면(`js/data.js`의 `RUN_ID_RE`)이 같은 식을 쓴다.

## 최상위 필드

| 필드 | 형식 | 설명 |
| --- | --- | --- |
| `schema_version` | 정수 | 지금은 `1` |
| `run_id` | 문자열 | 위 정규식에 맞는 값 |
| `created_at` | 문자열 | UTC, 밀리초, `Z`로 끝남. 예: `2026-10-28T08:53:02.891Z` |
| `kind` | 문자열 | `playground`(놀이터) 또는 `incident`(장애 분석, 아직 만들어지지 않음) |
| `title` | 문자열 | 입력의 앞 40자 |
| `exportable` | 참/거짓 | **기본 거짓.** 이 값을 참으로 바꾸는 기능은 아직 없다 |
| `redaction` | 객체 | `{applied: 참/거짓, note: 문자열}`. 지금은 `applied: false`, 마스킹 단계가 없다는 설명이 들어간다 |
| `environment` | 객체 | `ollama_version`(문자열 또는 null), `model`, `tokenizer`(`repo`, `revision`, `sha256`) |
| `settings` | 객체 | `temperature`, `num_predict`, `num_ctx`, 그리고 **seed를 줬을 때만** `seed`. 안 줬으면 키 자체가 없다 |
| `status` | 문자열 | `ok` / `error` / `aborted`(브라우저가 끊음) |
| `error` | 문자열 또는 null | 오류·중단 이유 |
| `stages` | 목록 | 단계 목록(아래) |

## 단계(stages)

모든 단계는 같은 틀을 가진다.

| 필드 | 설명 |
| --- | --- |
| `id` | 단계 id. 한 기록 안에서 중복되면 안 된다 |
| `status` | `ok` / `error` / `skipped` |
| `duration_ms` | 이 단계의 시간(ms) 또는 null |
| `evidence` | 필드별 증거 등급. `"*"`는 기본값이고 반드시 있어야 한다 |
| `output` | 단계 결과(단계마다 모양이 다르다) |
| `notes` | 문자열 목록. 오류·중단 이유나 값이 없는 이유 |

### 놀이터 6단계와 output

| 단계 | duration_ms | output 필드 |
| --- | --- | --- |
| `input` | null | `prompt`, `prompt_chars`, `prompt_bytes` |
| `tokenize` | 서버가 토큰화에 쓴 시간 | `text_chars`, `text_bytes`, `nfc_changed`, `token_count`, `partial_count`, `bytes_match`, `tokens[]`(`index`, `id`, `piece`, `hex`, `partial`, `special`, `fragment_of`) |
| `load` | `load_ms`와 같음 | `load_ms` (Ollama `load_duration`을 ms로) |
| `prefill` | `prompt_eval_ms`와 같음 | `prompt_eval_count`, `prompt_eval_ms`, `prompt_eval_cached_count`(Ollama가 안 주면 생략), `local_token_count`, `count_diff`(= `prompt_eval_count` − `local_token_count`) |
| `decode` | `eval_ms`와 같음 | `chunks[]`(`n`, `text`, `gap_ms`, `since_start_ms`), `chunk_count`, `eval_count`, `eval_ms`, `tok_s`, `done_reason`, `first_chunk_ms`, `total_ms` |
| `restore` | null | `server_joined`, `final_response`, `final_response_empty`, `chars`, `bytes` |

- `decode.total_ms`(Ollama `total_duration`을 ms로)는 시간 막대의 "기타" 구간을 계산하려고 추가한 필드다.
- `first_chunk_ms`와 `chunks[].since_start_ms`의 기준은 "서버가 Ollama에 요청을 보내기 시작한 시각"이고 연결 시간을 포함한다. 첫 조각의 `gap_ms`는 그 시각부터의 시간이다.
- `final_response`는 Ollama 스트림 마지막 줄의 `response` 필드를 그대로 담는다. 실측으로 항상 빈 문자열이어서 `final_response_empty`가 참이다.
- 브라우저가 이은 글과의 비교는 서버가 볼 수 없으므로 **기록에 없다**(live 놀이터에서 실행하는 동안만 화면에 보인다).

## 증거 등급 5가지

| 등급 | 화면 배지 | 뜻 |
| --- | --- | --- |
| `measured` | 실측 | 직접 재거나 Ollama·토크나이저에서 받은 값 |
| `computed` | 계산 | 잰 값으로 계산한 값(예: `tok_s`, `count_diff`) |
| `illustrative` | 설명용 | 이해를 돕는 예시. 실제 값이 아님 |
| `unverified` | 미확인 | 확인하지 못한 값 |
| `input` | 입력 | 사용자가 정한 값(계산이나 측정이 아님) |

`evidence`의 키는 필드 이름이고, 없는 필드는 `"*"` 값을 따른다. 현재 놀이터 기록은 `input`, `measured`, `computed`, `unverified`를 쓰고 `illustrative`는 쓰지 않는다.

- `input`(입력)은 다섯 번째 등급이다. 입력 단계에서 `prompt`와 설정(model, temperature, seed, num_predict, num_ctx)은 `input`, `prompt_chars`와 `prompt_bytes`는 `computed`로 적는다(`evidence`는 `{"*": "input", "prompt_chars": "computed", "prompt_bytes": "computed"}`).
- **`schema_version`은 1을 유지한다.** 허용하는 값(등급)만 늘어났고 필드 모양은 바뀌지 않았다. 입력 단계를 `computed`로 적은 이전 실행 기록도 그대로 검증을 통과하고 읽힌다(화면은 기록에 적힌 등급을 그대로 보여 준다). 모르는 등급은 계속 거부한다.

## 오류와 중단

그때까지 만든 단계는 그대로 두고, 실패한 단계는 `status: "error"`(notes에 이유), 그 뒤 단계는 `"skipped"`다. 전체 `status`는 `error` 또는 `aborted`다.

- Ollama에 연결하지 못했거나 조각이 하나도 오기 전에 멈추면, 올리기·읽기 중 어디서 멈췄는지 알 수 없어 **`load`를 실패 지점**으로 둔다(notes에 이유를 적는다). `prefill`, `decode`, `restore`는 `skipped`.
- 조각이 일부 온 뒤에 멈추면, 통계는 마지막 줄에 오므로 값이 없다. `load`와 `prefill`은 `ok`이지만 `output`이 비어 있고 증거 등급이 `unverified`다. `decode`는 `error`이고 받은 조각(`chunks`, `chunk_count`, `first_chunk_ms`)까지만 담는다. `restore`는 `skipped`.

## 쓰기 규칙

- 실행 하나당 파일 하나. 같은 폴더에 임시 파일로 쓰고 `os.replace`로 바꿔 넣는다(반쯤 쓰인 파일이 읽히지 않게). 실패하면 임시 파일을 지운다.
- 기록 파일을 먼저 쓰고 그다음 `index.json`을 갱신한다. `index.json`은 잠금 아래에서 같은 방식으로 원자적으로 쓴다.
- `index.json`은 `[{run_id, created_at, kind, title, model, status, exportable}]` 최신순 최대 200개다. 200개를 넘으면 목록에서만 빠지고 기록 파일은 지우지 않는다.
- 직렬화한 기록이 2MB를 넘으면 쓰지 않고 표준 오류에만 남긴다.
- 쓰기 전에 항상 `validate_bundle()`을 호출한다. 문제가 있으면 쓰지 않고 표준 오류에만 남긴다.
- 기록 실패(검증 실패, 크기 초과, 디스크 오류 포함)는 응답 스트림을 망치지 않는다.
- `/api/run`은 `start`와 `done`(또는 `error`) 이벤트에 `run_id`를 담는다. 기록은 `done`/`error` 이벤트를 내보내기 **전에** 쓴다. 브라우저가 끊으면(aborted) 스트림이 닫힐 때 쓴다.
- 토큰화 같은 계산은 서버가 `/api/run` 안에서 한 번 하고 기록에 넣는다. 브라우저가 따로 계산한 값은 저장하지 않는다.

## 읽기 경로

- `GET /data/index.json`: 아직 실행이 없으면 `[]`을 돌려준다.
- `GET /data/runs/<run_id>.json`: `run_id`가 정규식에 맞지 않으면 404. 파일 시스템을 보지도 않는다.
- `/data` 아래 다른 파일(옛 `traces.jsonl` 포함)은 404다.
- `GET /js/…`, `/css/…`, `/content/…`는 `static/` 아래의 `.js`, `.css`, `.json`만 준다.

## 지금 지키는 보안 규칙

- `exportable`은 기본 거짓이고 이 값을 참으로 바꾸는 기능은 없다. 공개 내보내기 기능과 내보내기 검사는 아직 만들지 않았다.
- `run_id`는 정규식으로만 받는다(서버와 화면 모두). 경로를 이루는 다른 입력은 받지 않는다.
- **마스킹은 아직 없다.** 사용자가 직접 입력한 글이 `prompt`와 `title`, `chunks`, `server_joined`에 그대로 들어간다. 그래서 `redaction.applied`는 거짓이고, 공개하기 전에 마스킹과 내보내기 검사가 따로 필요하다.
- 화면은 모델이 만든 글과 기록 안의 문자열을 `textContent`로만 넣는다(`innerHTML` 금지). 응답 헤더 `Content-Security-Policy`는 `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'`이다.

## 실제 기록 예시 (앞 60줄)

실제 실행 한 번(`r-20261029-025809-06a1`, 기본 문장, temperature 0.2, seed 42, num_predict 200, num_ctx 4096)에서 만든 기록의 앞 60줄이다. 전체는 763줄이다.

```json
{
  "schema_version": 1,
  "run_id": "r-20261029-025809-06a1",
  "created_at": "2026-10-29T02:58:09.947Z",
  "kind": "playground",
  "title": "웹 서비스에서 1분 동안 요청의 37%가 HTTP 500 에러였다. 원인",
  "exportable": false,
  "redaction": {
    "applied": false,
    "note": "사용자가 직접 입력한 글. 마스킹 단계 없음(S0)"
  },
  "environment": {
    "ollama_version": "0.40.2",
    "model": "qwen2.5:7b",
    "tokenizer": {
      "repo": "Qwen/Qwen2.5-7B-Instruct",
      "revision": "a09a35458c702b33eeacc393d103063234e8bc28",
      "sha256": "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"
    }
  },
  "settings": {
    "temperature": 0.2,
    "num_predict": 200,
    "num_ctx": 4096,
    "seed": 42
  },
  "status": "ok",
  "error": null,
  "stages": [
    {
      "id": "input",
      "status": "ok",
      "duration_ms": null,
      "evidence": {
        "*": "input",
        "prompt_chars": "computed",
        "prompt_bytes": "computed"
      },
      "output": {
        "prompt": "웹 서비스에서 1분 동안 요청의 37%가 HTTP 500 에러였다. 원인 후보 두 가지를 한국어로 한 줄씩 써라.",
        "prompt_chars": 63,
        "prompt_bytes": 131
      },
      "notes": []
    },
    {
      "id": "tokenize",
      "status": "ok",
      "duration_ms": 190.988,
      "evidence": {
        "*": "measured",
        "bytes_match": "computed",
        "token_count": "computed"
      },
      "output": {
        "text_chars": 63,
        "text_bytes": 131,
        "nfc_changed": false,
        "token_count": 43,
        "partial_count": 5,
```
