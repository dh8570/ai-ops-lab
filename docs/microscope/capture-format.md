# 현미경 캡처 형식 (v0)

## 목적

현미경 엔진(`microscope/engine/`)은 Qwen2.5-7B-Instruct(bf16)를 CPU에서 직접 계산한다. Prefill(질문을 한 번에 읽는 단계)과
Decode(토큰을 하나씩 이어 쓰는 단계)의 층마다 중간 값을 모두 꺼내 파일로 남기고, 단계마다 어떤 토큰을 어떻게 골랐는지(뽑기)도 기록한다.
학습용 화면(M3·M4)은 이 파일을 읽어 "모델 안에서 무슨 일이 일어나는가"를 보여 준다.

이 값이 믿을 만하다는 근거는 검증이다: 같은 입력에서 우리 계산의 층 출력·K·V·어텐션 가중치·로짓이 Hugging Face 공식 구현
(transformers 5.19.0 Qwen2, eager, DynamicCache)과 Prefill·Decode 모두 비트 단위로 같다(`python -m microscope.verify`,
결과는 실행 폴더의 `verification_prefill.json`, `verification_decode.json`).

## 만들기와 읽기

```bash
# 만들기 (기본 입력, 탐욕 8토큰, 결과는 ~/microscope-runs/<run_id>/)
python -m microscope.run [--text "사용자 글"] [--out ~/microscope-runs] [--max-new 8]
# 뽑기 (temperature > 0이면 --seed 필수)
python -m microscope.run --temperature 0.8 --top-k 20 --top-p 0.9 --seed 42
# 검증 (HF와 비교해 그 폴더에 verification_prefill.json, verification_decode.json을 씀)
python -m microscope.verify --run ~/microscope-runs/<run_id>
```

| 옵션 | 기본 | 뜻 |
| --- | --- | --- |
| `--max-new` | 8 | 새 토큰 수의 상한(1..32). 첫 토큰은 Prefill 끝에서 나오므로 Decode 단계 수는 새 토큰 수 − 1 |
| `--temperature` | 0 | 0이면 탐욕 |
| `--top-k`, `--top-p` | 없음 | 아래 "뽑기" |
| `--seed` | 없음 | 난수 시작 번호. temperature > 0이면 필수 |
| `--logits-path` | `f32` | 뽑기에 쓰는 로짓. `bf16`은 검증용(bf16 로짓의 탐욕, temperature 0에서만) |

프롬프트 + 새 토큰 수가 160을 넘으면 엔진이 ValueError로 멈춘다.

```python
from microscope.engine.capture import load_manifest, read_kv_cache, read_tensor

run = "/home/ubuntu/microscope-runs/r-20261029-072238-7ed8"
m = load_manifest(run)
x = read_tensor(run, "L14.resid2", manifest=m)        # Prefill [T, H] float32 numpy
a = read_tensor(run, "D3.L5.attn", (2,), manifest=m)  # Decode 3단계, 5층 2번 헤드의 어텐션 [1, Tk]
k, v = read_kv_cache(run, layer=5, step=3, manifest=m) # Decode 3단계 뒤 5층의 KV 캐시 [Hk, T+3, D]
```

`read_tensor(run_dir, name, index=None, manifest=None)`는 항상 float32 numpy를 돌려준다. bf16 비트는 float32의 위 16비트로 옮겨
정확히 같은 값으로 바꾼다. `index`는 numpy 인덱싱 그대로이고, 파일을 memmap으로 열어 필요한 부분만 읽는다.
manifest를 여러 번 읽지 않으려면 `manifest=`에 `load_manifest` 결과를 넘긴다. `file` 필드가 없는 기록(M2-a)은 `tensors/prefill.bin`으로 읽는다.

## 폴더 구조

실행 하나 = 폴더 하나. 레포 밖에 둔다.

```
~/microscope-runs/<run_id>/
├── manifest.json               설명서: 모델, 환경, 프롬프트, 생성 설정과 결과, 텐서 목록(파일·위치·모양·통계), 시간, 메모리
├── tensors/prefill.bin         Prefill 텐서를 이어 붙인 이진 파일
├── tensors/decode.bin          Decode 단계 텐서를 단계 순서로 이어 붙인 이진 파일(Decode 단계가 없으면 없음)
├── verification_prefill.json   (검증을 돌린 경우) Prefill의 HF 비교 표
└── verification_decode.json    (검증을 돌린 경우) Decode의 HF 비교 표
```

`run_id`는 `r-YYYYMMDD-HHMMSS-xxxx`(UTC 시각, `xxxx`는 무작위 16진수 4자리).

## manifest.json 필드

| 필드 | 뜻 |
| --- | --- |
| `schema_version` | 0 |
| `run_id`, `created_at` | 실행 이름, 만든 시각(UTC, ISO 8601, `Z`로 끝남) |
| `kind`, `evidence` | 둘 다 `"microscope"` |
| `model` | `repo`, `revision`(모델 폴더의 REVISION.txt), `layers`, `hidden_size`, `heads`, `kv_heads`, `head_dim`, `intermediate_size`, `vocab_size` (config.json에서 읽음) |
| `environment` | `torch`, `transformers` 버전, `threads`(MICROSCOPE_THREADS), `dtype`(`"bf16"`), `cpu_capability`(예: AVX2) |
| `prompt` | `user_text`, `template_string`(채팅 틀을 씌운 전체 글), `token_ids`, `tokens`, `n_prefix`(사용자 글 앞 틀 토큰 수), `n_suffix`(뒤 틀 토큰 수) |
| `prompt.tokens[]` | `index`, `id`, `piece`(그 토큰 하나를 글로 풀어 쓴 것. 글자 일부만 담은 바이트 조각이면 `�`), `hex`(토큰의 원래 바이트) |
| `generation` | `max_new`, `stop_reason`(`"eos"` 또는 `"length"`), `logits_path`(`"f32"` 또는 `"bf16"`), `eos_token_ids`(generation_config.json에서 읽음), `sampling{temperature, top_k, top_p, seed, sampler: "microscope-v0"}` |
| `generated[]` | 새 토큰마다 하나. 아래 "생성 기록" |
| `generated_text` | 새 토큰을 이어 붙여 글로 바꾼 것 |
| `phases[]` | `{id: "prefill", tokens_in, tokens_total, seconds}`, 그 뒤 Decode 단계마다 `{id: "decode:j", step: j, tokens_in: 1, tokens_total, seconds}`. seconds는 캡처 복사 포함 |
| `tensors[]` | 아래 "텐서 항목" |
| `mlp_summary[]` | 아래 "MLP 요약" |
| `topk` | Prefill 끝(첫 토큰을 고른 위치)의 상위 50개: `bf16_path`, `float32_path`. `generated[0].top50`과 같은 값(M2-a와 같은 필드) |
| `timing` | `load_s`(가중치 적재), `prefill_s`, `decode_s`(Decode 단계 합), `capture_write_s`(통계 계산 + 파일 쓰기) |
| `memory` | `rss_peak_gib`: 쓰기 직전까지 프로세스 RSS 최대치(VmHWM) |

### 생성 기록 (`generated[]`)

| 필드 | 뜻 |
| --- | --- |
| `n` | 새 토큰 번호(1부터) |
| `source` | 이 토큰을 고른 로짓이 나온 곳. `"prefill"`(첫 토큰) 또는 `"decode:j"`(Decode 단계 j의 출력, 이때 n = j + 1) |
| `id`, `text`, `hex` | 고른 토큰의 번호, 글자, 원래 바이트 |
| `argmax` | `{f32, bf16}`: 그 위치에서 float32 로짓과 bf16 로짓 각각의 탐욕 선택(같은 값이면 번호 작은 토큰) |
| `top50` | `{f32: [...], bf16: [...]}`: 로짓 상위 50개 `{id, text, logit, prob, logprob}`(확률은 float32 softmax, temperature 적용 전) |
| `sampling` | `mode`(`"greedy"`, `"greedy_bf16"`, `"sample"`), `u`(난수, 탐욕이면 null), `kept_after_temperature`, `kept_after_top_k`, `kept_after_top_p`(각 단계 뒤 남은 후보 수, 탐욕이면 null), `candidates`(최종 후보 상위 50개 `{id, text, prob, cum}`, 확률 큰 순서), `chosen_index`(고른 토큰의 최종 후보 안 자리) |

### 텐서 항목

`{name, phase, step, layer, shape, dtype, file, offset, nbytes, stats}`

- `phase`: `"prefill"` 또는 `"decode"`. `step`: Decode 단계 번호 j(Prefill은 `null`). `layer`: 층 번호, 층 밖 텐서는 `null`.
- `dtype`: `"bf16"`은 uint16 원본 비트(리틀 엔디안), `"f32"`는 float32 그대로(리틀 엔디안).
- `file`: `"tensors/prefill.bin"` 또는 `"tensors/decode.bin"`. `offset`, `nbytes`는 그 파일 안의 시작 위치와 길이(바이트). 파일마다 텐서가 빈틈없이 순서대로 이어 붙는다.
- `stats`: `{min, max, mean, std, l2}`. 값을 float32로 바꾼 뒤 float64로 누적해 계산한다(마스크 값의 제곱이 넘치지 않게). `std`는 모표준편차.

## 텐서 이름·모양·뜻

기호: T = 프롬프트 토큰 수, H = hidden_size(3584), Hq = 질문 헤드 수(28), Hk = KV 헤드 수(4), D = head_dim(128),
Tk = 키 개수(Prefill에서는 T, Decode 단계 j에서는 T + j). 자료형은 `logits_last_f32`만 f32이고 나머지는 모두 bf16이다.

Prefill 텐서(아래 모양의 첫 줄)와 Decode 단계 j의 텐서(`D{j}.` 접두어, 토큰이 하나라 T 자리가 1)는 이름과 뜻이 같다.

| 이름 | Prefill 모양 | Decode `D{j}.` 모양 | 뜻 |
| --- | --- | --- | --- |
| `embed` | [T,H] | [1,H] | 토큰 번호를 표에서 찾아 바꾼 첫 벡터 |
| `L{l}.norm1` | [T,H] | [1,H] | 층 입력을 크기만 고르게 맞춘 값(input_layernorm) |
| `L{l}.q_pre` | [Hq,T,D] | [Hq,1,D] | 질문(Query) 벡터, 위치 회전 전 |
| `L{l}.k_pre` | [Hk,T,D] | [Hk,1,D] | 열쇠(Key) 벡터, 위치 회전 전 |
| `L{l}.v` | [Hk,T,D] | [Hk,1,D] | 내용(Value) 벡터(이번 토큰 것만) |
| `L{l}.q` | [Hq,T,D] | [Hq,1,D] | RoPE로 위치만큼 회전한 질문 벡터 |
| `L{l}.k` | [Hk,T,D] | [Hk,1,D] | RoPE로 회전한 열쇠 벡터(이번 토큰 것만, 캐시에 붙는 값) |
| `L{l}.scores` | [Hq,T,Tk] | [Hq,1,Tk] | 질문과 열쇠의 닮은 정도 × 1/√D에 마스크를 더한 소프트맥스 전 점수 |
| `L{l}.attn` | [Hq,T,Tk] | [Hq,1,Tk] | 소프트맥스 뒤 어텐션 가중치(행마다 합 1, "어느 토큰을 얼마나 보는가") |
| `L{l}.ctx` | [Hq,T,D] | [Hq,1,D] | 어텐션 가중치로 V를 섞은 헤드별 결과 |
| `L{l}.attn_out` | [T,H] | [1,H] | 헤드 결과를 이어 붙여 출력 투영(o_proj)한 값 |
| `L{l}.resid1` | [T,H] | [1,H] | 층 입력 + attn_out (첫 번째 잔차) |
| `L{l}.norm2` | [T,H] | [1,H] | resid1을 다시 고르게 맞춘 값(post_attention_layernorm) |
| `L{l}.mlp_out` | [T,H] | [1,H] | MLP(gate·up·SiLU·down)의 출력 |
| `L{l}.resid2` | [T,H] | [1,H] | resid1 + mlp_out (= 층 출력, 다음 층 입력) |
| `final_norm` | [T,H] | [1,H] | 마지막 층 출력을 고르게 맞춘 값(model.norm) |
| `logits_last_bf16` | [vocab] | [vocab] | 마지막 위치의 다음 토큰 점수, bf16 경로(공식 구현과 같은 값) |
| `logits_last_f32` | [vocab] | [vocab] | 같은 점수를 float32로 다시 계산한 값(입력은 bf16 경로의 final_norm 마지막 행) |

파일 안 순서: 단계마다 `embed`, 층 0의 14개(위 표 순서), 층 1, …, 층 27, `final_norm`, `logits_last_bf16`, `logits_last_f32`.
한 단계에 396개. prefill.bin에 Prefill 396개, decode.bin에 D1, D2, … 순서로 단계마다 396개.

Decode 단계 j의 입력은 새 토큰 j(`generated[j-1]`), 위치는 T + j − 1이고, 출력 로짓으로 새 토큰 j+1을 고른다.
Decode의 `scores`에는 공식 구현과 같이 0으로 채운 마스크를 더한다(질문 하나가 캐시의 모든 키와 자기 자신을 본다).

### KV 캐시 복원

KV 캐시 전체는 단계마다 저장하지 않는다. Decode 단계 j를 마친 뒤 층 l의 캐시는
Prefill의 `L{l}.k`·`L{l}.v` 뒤에 `D1.L{l}.k`, …, `D{j}.L{l}.k`(값도 같음)를 토큰 축(두 번째 축)으로 이어 붙인 것이다.
`read_kv_cache(run_dir, layer, step)`가 이것을 돌려준다(step 0이면 Prefill만). 검증(V9)에서 마지막 단계 뒤의 이 값이
HF DynamicCache와 28층 모두 비트 단위로 같았다.

### MLP 요약 (`mlp_summary[]`)

MLP 안쪽 값은 층마다 [T, intermediate_size(18944)]라 커서 통째로 저장하지 않는다. 단계·층마다 하나:

| 필드 | 뜻 |
| --- | --- |
| `step` | Decode 단계 번호(Prefill은 `null`) |
| `layer` | 층 번호 |
| `gate`, `up`, `act` | 각 값의 `{min, max, mean, std, l2}`. `gate` = gate_proj 출력, `up` = up_proj 출력, `act` = SiLU(gate) × up (down_proj로 들어가는 값) |
| `act_top8` | 토큰마다 `act`의 절댓값이 큰 상위 8개 `[위치(0..18943), 값]`. 바깥 목록 길이는 Prefill T, Decode 1 |

## 뽑기 (sampler "microscope-v0")

우리 자체 구현이다. Ollama·llama.cpp·HF의 구현과 같다고 주장하지 않는다. 입력은 float32 로짓(`logits_path` 기본 `f32`).

- temperature == 0: 탐욕. float32 로짓의 최댓값 토큰(같은 값이면 번호가 작은 토큰). 난수를 쓰지 않는다.
- temperature > 0: 다음 순서로 고른다.
  1. p = softmax(로짓 / temperature)를 float64로 계산
  2. 확률 큰 순서(같으면 번호 작은 순서)로 정렬
  3. top_k가 있으면 앞의 k개만 남김
  4. 남은 것을 합 1로 맞춘 뒤, top_p가 있으면 "그 앞까지의 누적이 top_p 미만"인 토큰만 남김(첫 토큰은 항상 남음)
  5. 남은 것을 합 1로 다시 맞춤
  6. 난수 u(`torch.Generator(cpu).manual_seed(seed)`의 `torch.rand(1, float64)`)를 뽑아 누적이 처음으로 u보다 **커지는** 토큰을 고름
     (u가 누적 경계와 정확히 같으면 다음 토큰. 반올림으로 마지막 누적이 u 이하이면 마지막 후보)
- Generator는 실행마다 한 번 만들어 단계마다 이어서 쓴다. seed와 설정이 같으면 같은 토큰열이 나온다(실측: seed 42 두 번, 캡처 파일까지 바이트 동일).
- **단순화 주의**: temperature → top_k → top_p 순서와 위의 경계 규칙은 이 엔진이 정한 것이다. 실제 구현들은 순서(예: top_k를 temperature보다 먼저),
  반올림, 다시 맞추는 시점, 난수 생성기가 서로 달라 같은 seed라도 같은 결과를 내지 않는다. 반복 벌점(repetition_penalty) 같은 다른 처리는 하지 않는다.
- 로짓에 NaN이나 +inf가 있으면 ValueError.

## 저장 크기

기본 입력(T = 72), 새 토큰 8개(Decode 7단계) 실측:

| 파일 | 크기 |
| --- | --- |
| `tensors/prefill.bin` | 154,450,944 바이트(147.3MiB) |
| `tensors/decode.bin` | 21,401,856 바이트(20.4MiB) = 단계당 3,057,408 바이트(Tk가 늘어도 `scores`·`attn`만 조금 커짐) |
| `manifest.json` | 2,221,699 바이트 |

쓰기 0.30초. Prefill 층 하나에 약 5.3MiB이고 대부분 T에 비례한다(`scores`·`attn`은 T²에 비례).

## 지금 알려진 한계

- `scores`의 Prefill 미래 칸에는 공식 구현과 같은 마스크 값(bf16 최솟값 −3.39×10³⁸)이 그대로 들어 있다. 그래서 Prefill `scores`의 `stats`(min, mean, std, l2)는
  마스크 칸에 지배되어 의미가 없다. 화면에서는 위치 i에 대해 키 0..i만 써야 한다. Decode의 `scores`에는 미래 칸이 없다.
- 로짓은 각 단계의 마지막 위치만 저장한다.
- bf16 경로의 로짓은 0.125 간격 계단 위에 있어(값 16~32 구간) 동점이 생길 수 있다(실측: 탐욕 실행 Decode 2단계의 3·4위). 순위·확률을 자세히 볼 때는 `logits_last_f32`를 쓴다.
- `top50`의 확률은 temperature 적용 전 값이다. 뽑기에 실제로 쓴 확률은 `sampling.candidates`에 있다.
- 마지막으로 고른 토큰은 다시 Decode하지 않으므로 그 토큰의 K·V는 캐시에 없다(새 토큰 n개면 Decode n − 1단계).
- `rss_peak_gib`는 캡처 쓰기 직전까지의 최대치다.
- 가중치는 safetensors 파일을 읽기 전용 메모리 매핑으로 올리므로 RSS 대부분이 파일 매핑(RssFile)이다. `load_s`는 이미 페이지 캐시에 올라와 있으면 1초 미만이고, 디스크에서 처음 읽으면 더 길다.
- 한 번에 한 입력(배치 1)만 계산한다. sliding window, rope_scaling은 구현하지 않았다(config에 있으면 멈춘다).
- 형식 v0이다. 필드가 바뀌면 `schema_version`을 올린다(이번 M2-b 추가는 기존 필드를 바꾸지 않고 더하기만 했으므로 0 유지).
