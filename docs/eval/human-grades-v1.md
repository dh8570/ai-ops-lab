# 사람 채점 v1 (A1-T5b)

v1 raw 파일(`docs/eval/raw/eval-20261103T043035Z.jsonl`)의 prod 1번 호출 10개를 PM이 사람 기준으로 채점. 정확성·유용성 0~2.

`auto`는 같은 호출에 대한 v1 자동 판정(pass/fail)이다.

| scenario | condition | accuracy | usefulness | auto |
|---|---|---|---|---|
| s1_error_spike | no_runbook | 1 | 1 | fail |
| s1_error_spike | runbook | 1 | 1 | pass |
| s2_db_down | no_runbook | 0 | 0 | fail |
| s2_db_down | runbook | 1 | 2 | pass |
| s3_latency | no_runbook | 0 | 0 | pass |
| s3_latency | runbook | 2 | 2 | pass |
| s4_app_down | no_runbook | 1 | 0 | fail |
| s4_app_down | runbook | 2 | 2 | pass |
| s5_weak_signal | no_runbook | 1 | 1 | pass |
| s5_weak_signal | runbook | 1 | 1 | pass |

## 자동 판정과 어긋난 칸

- s3_latency / no_runbook
- s5_weak_signal / no_runbook
- s1_error_spike / runbook
- s5_weak_signal / runbook
