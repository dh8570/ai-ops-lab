# 현미경 캡처 형식 (v0)

## 목적

현미경 엔진(`microscope/engine/`)은 Qwen2.5-7B-Instruct(bf16)의 Prefill(질문을 한 번에 읽는 단계)을 CPU에서 직접 계산하면서
층마다 중간 값을 모두 꺼내 파일로 남긴다. 학습용 화면(M3·M4)은 이 파일을 읽어 "모델 안에서 무슨 일이 일어나는가"를 보여 준다.

이 값이 믿을 만하다는 근거는 검증이다: 같은 입력에서 우리 계산의 층 출력·K·V·어텐션 가중치·로짓이 Hugging Face 공식 구현
(transformers 5.19.0 Qwen2, eager)과 비트 단위로 같다(`python -m microscope.verify`, 결과는 실행 폴더의 `verification_prefill.json`).

## 만들기와 읽기

```bash
# 만들기 (기본 입력, 결과는 ~/microscope-runs/<run_id>/)
python -m microscope.run [--text "사용자 글"] [--out ~/microscope-runs]
# 검증 (HF와 비교해 그 폴더에 verification_prefill.json을 씀)
python -m microscope.verify --run ~/microscope-runs/<run_id>
```

```python
from microscope.engine.capture import load_manifest, read_tensor

run = "/home/ubuntu/microscope-runs/r-20261029-064253-770e"
m = load_manifest(run)
x = read_tensor(run, "L14.resid2")             # [T, H] float32 numpy
a = read_tensor(run, "L3.attn", (5,))          # 3층 5번 헤드의 어텐션 [T, Tk]
q = read_tensor(run, "L0.q", (2, 10))          # 0층 2번 헤드, 10번 토큰의 질문 벡터 [D]
```

`read_tensor(run_dir, name, index=None, manifest=None)`는 항상 float32 numpy를 돌려준다. bf16 비트는 float32의 위 16비트로 옮겨
정확히 같은 값으로 바꾼다. `index`는 numpy 인덱싱 그대로이고, 파일을 memmap으로 열어 필요한 부분만 읽는다.
manifest를 여러 번 읽지 않으려면 `manifest=`에 `load_manifest` 결과를 넘긴다.

## 폴더 구조

실행 하나 = 폴더 하나. 레포 밖에 둔다.

```
~/microscope-runs/<run_id>/
├── manifest.json               설명서: 모델, 환경, 프롬프트, 텐서 목록(위치·모양·통계), 상위 50개, 시간, 메모리
├── tensors/prefill.bin         모든 텐서를 이어 붙인 이진 파일
└── verification_prefill.json   (검증을 돌린 경우) HF와의 층별 비교 표
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
| `phases[]` | `{id: "prefill", tokens_in, tokens_total, seconds}` |
| `tensors[]` | 아래 "텐서 항목" |
| `mlp_summary[]` | 아래 "MLP 요약" |
| `topk` | `bf16_path`, `float32_path`: 마지막 위치의 상위 50개 `{id, text, logit, prob, logprob}`. 확률은 float32 softmax |
| `timing` | `load_s`(가중치 적재), `prefill_s`(Prefill 계산 + 캡처 복사), `capture_write_s`(통계 계산 + 파일 쓰기) |
| `memory` | `rss_peak_gib`: 쓰기 직전까지 프로세스 RSS 최대치(VmHWM) |

### 텐서 항목

`{name, phase, layer, shape, dtype, offset, nbytes, stats}`

- `phase`: `"prefill"`. `layer`: 층 번호, 층 밖 텐서는 `null`.
- `dtype`: `"bf16"`은 uint16 원본 비트(리틀 엔디안), `"f32"`는 float32 그대로(리틀 엔디안).
- `offset`, `nbytes`: prefill.bin 안의 시작 위치와 길이(바이트). 텐서는 빈틈없이 순서대로 이어 붙는다.
- `stats`: `{min, max, mean, std, l2}`. 값을 float32로 바꾼 뒤 float64로 누적해 계산한다(마스크 값의 제곱이 넘치지 않게). `std`는 모표준편차.

## 텐서 이름·모양·뜻

기호: T = 토큰 수, H = hidden_size(3584), Hq = 질문 헤드 수(28), Hk = KV 헤드 수(4), D = head_dim(128), Tk = 키 개수(Prefill에서는 T).
자료형은 `logits_last_f32`만 f32이고 나머지는 모두 bf16이다.

| 이름 | 모양 | 뜻 |
| --- | --- | --- |
| `embed` | [T,H] | 토큰 번호를 표에서 찾아 바꾼 첫 벡터 |
| `L{l}.norm1` | [T,H] | 층 입력을 크기만 고르게 맞춘 값(input_layernorm) |
| `L{l}.q_pre` | [Hq,T,D] | 질문(Query) 벡터, 위치 회전 전 |
| `L{l}.k_pre` | [Hk,T,D] | 열쇠(Key) 벡터, 위치 회전 전 |
| `L{l}.v` | [Hk,T,D] | 내용(Value) 벡터 |
| `L{l}.q` | [Hq,T,D] | RoPE로 위치만큼 회전한 질문 벡터 |
| `L{l}.k` | [Hk,T,D] | RoPE로 회전한 열쇠 벡터(= 이 층의 K 캐시) |
| `L{l}.scores` | [Hq,T,Tk] | 질문과 열쇠의 닮은 정도 × 1/√D, 미래 칸에는 마스크를 더한 소프트맥스 전 점수 |
| `L{l}.attn` | [Hq,T,Tk] | 소프트맥스 뒤 어텐션 가중치(행마다 합 1, "어느 토큰을 얼마나 보는가") |
| `L{l}.ctx` | [Hq,T,D] | 어텐션 가중치로 V를 섞은 헤드별 결과 |
| `L{l}.attn_out` | [T,H] | 헤드 결과를 이어 붙여 출력 투영(o_proj)한 값 |
| `L{l}.resid1` | [T,H] | 층 입력 + attn_out (첫 번째 잔차) |
| `L{l}.norm2` | [T,H] | resid1을 다시 고르게 맞춘 값(post_attention_layernorm) |
| `L{l}.mlp_out` | [T,H] | MLP(gate·up·SiLU·down)의 출력 |
| `L{l}.resid2` | [T,H] | resid1 + mlp_out (= 층 출력, 다음 층 입력) |
| `final_norm` | [T,H] | 마지막 층 출력을 고르게 맞춘 값(model.norm) |
| `logits_last_bf16` | [vocab] | 마지막 위치의 다음 토큰 점수, bf16 경로(공식 구현과 같은 값) |
| `logits_last_f32` | [vocab] | 같은 점수를 float32로 다시 계산한 값(입력은 bf16 경로의 final_norm 마지막 행) |

파일 안 순서: `embed`, 층 0의 14개(위 표 순서), 층 1, …, 층 27, `final_norm`, `logits_last_bf16`, `logits_last_f32`. 모두 396개.

### MLP 요약 (`mlp_summary[]`)

MLP 안쪽 값은 층마다 [T, intermediate_size(18944)]라 커서 통째로 저장하지 않는다. 층마다 하나:

| 필드 | 뜻 |
| --- | --- |
| `layer` | 층 번호 |
| `gate`, `up`, `act` | 각 값의 `{min, max, mean, std, l2}`. `gate` = gate_proj 출력, `up` = up_proj 출력, `act` = SiLU(gate) × up (down_proj로 들어가는 값) |
| `act_top8` | 토큰마다 `act`의 절댓값이 큰 상위 8개 `[위치(0..18943), 값]`. 바깥 목록 길이 T |

## 저장 크기

기본 입력(T = 72) 실측: `prefill.bin` 154,450,944 바이트(147.3MiB), `manifest.json` 863,743 바이트. 쓰기 0.17초.
층 하나에 약 5.3MiB이고, 대부분 T에 비례한다(`scores`·`attn`은 T²에 비례).

## 지금 알려진 한계

- Prefill만 담는다. Decode(토큰 하나씩 이어 쓰기), 뽑기(sampling)는 다음 단계(M2-b)다.
- `scores`의 미래 칸에는 공식 구현과 같은 마스크 값(bf16 최솟값 −3.39×10³⁸)이 그대로 들어 있다. 그래서 `scores`의 `stats`(min, mean, std, l2)는
  마스크 칸에 지배되어 의미가 없다. 화면에서는 위치 i에 대해 키 0..i만 써야 한다.
- 로짓은 마지막 위치만 저장한다(다른 위치는 저장하지 않음).
- bf16 경로의 로짓은 0.125 간격 계단 위에 있다(값 16~32 구간에서 bf16이 표현할 수 있는 간격). 순위·확률을 자세히 볼 때는 `logits_last_f32`를 쓴다.
- `rss_peak_gib`는 캡처 쓰기 직전까지의 최대치다.
- 가중치는 safetensors 파일을 읽기 전용 메모리 매핑으로 올리므로 RSS 대부분이 파일 매핑(RssFile)이다. `load_s`는 이미 페이지 캐시에 올라와 있으면 1초 미만이고, 디스크에서 처음 읽으면 더 길다.
- 한 번에 한 입력(배치 1)만 계산한다. sliding window, rope_scaling은 구현하지 않았다(config에 있으면 멈춘다).
- 형식 v0이다. 필드가 바뀌면 `schema_version`을 올린다.
