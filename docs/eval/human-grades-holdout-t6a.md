# 사람 채점 기록: T6a 보류 시나리오

T6a 보류 시나리오 s8~s11의 warm 1번 호출 20개와 s5 두 개를 PM이 사람 기준으로 채점. 정확성·유용성 0~2

```csv
scenario,condition,accuracy,usefulness
s8_db_partial_fail,nr_v1,1,1
s8_db_partial_fail,all_v1,2,1
s8_db_partial_fail,all_v2,2,2
s8_db_partial_fail,match_v1,1,2
s8_db_partial_fail,match_v2,1,1
s9_saturation,nr_v1,0,0
s9_saturation,all_v1,1,2
s9_saturation,all_v2,2,2
s9_saturation,match_v1,1,2
s9_saturation,match_v2,2,2
s10_5xx_no_detail,nr_v1,1,1
s10_5xx_no_detail,all_v1,0,1
s10_5xx_no_detail,all_v2,2,1
s10_5xx_no_detail,match_v1,1,1
s10_5xx_no_detail,match_v2,0,0
s11_probe_timeout,nr_v1,1,0
s11_probe_timeout,all_v1,2,2
s11_probe_timeout,all_v2,2,2
s11_probe_timeout,match_v1,2,2
s11_probe_timeout,match_v2,2,2
s5_weak_signal,all_v1,0,0
s5_weak_signal,all_v2,2,1
```

## 자동 판정과 사람 판정이 어긋난 칸

- s5 all_v2: 사람 2/1, 자동 strict_v3 탈락. 키워드 "감소"를 "줄어든"이 놓침.
- s10 all_v2: 사람 2/1, 자동 0. 금지어 "DB"가 런북 문장의 "db"에 걸림.
