# 사람 채점 (s6, s7) (A1-T5c 후속)

`docs/eval/human-sheet-s6-s7.md`의 prod 1번 호출 4개를 PM이 사람 기준으로 채점. 정확성·유용성 0~2.
이 두 시나리오는 v2 키워드와 strict_v3를 정할 때 사람 채점을 하지 않은 새 데이터다.

| scenario | condition | accuracy | usefulness | 이유 |
|---|---|---|---|---|
| s6_new_path_error | no_runbook | 2 | 2 | 근거의 /checkout ValueError를 정확히 짚었고 확인 항목(코드, 로그)을 바로 실행할 수 있다 |
| s6_new_path_error | runbook | 1 | 1 | 첫 원인은 맞다. "/ 경로에서도 500", "DB 연결 문제"는 근거(/ 150건 모두 2xx)와 모순이며 런북의 DB 항목을 옮겨 적었다 |
| s7_db_slow_combo | no_runbook | 0 | 1 | 원인 칸에 규칙 이름 되풀이와 깨진 글(checksifestyles)이 들어갔다. 확인 항목에 OperationalError 조사가 있어 유용성은 약간 있다 |
| s7_db_slow_combo | runbook | 1 | 1 | 첫 원인(DB 연결)은 맞다. 둘째 원인(워커 포화로 /healthz 불가)은 근거(프로브 정상)와 모순이고 둘째 확인이 지연 쪽으로 안내한다 |

## 관찰

- 런북에 없는 경로(s6)에서는 런북이 없는 조건이 더 좋았다(2/2 대 1/1). 런북의 다른 장애 항목을 옮겨 적는 것이 원인이다.
- 자동 판정(strict_v3)은 이 4칸에서 방향이 사람과 같았다(s6 no_runbook만 통과 다수, 나머지 0/11).
- 같은 설정 3회의 글은 warm(먼저 한 번 보낸 뒤)에서 7/7로 같고 prod에서는 달랐다. 캐시 상태를 맞추면 재현된다.
- 시나리오마다 한 번의 호출이라 표본이 작다. 방향만 믿는다.
