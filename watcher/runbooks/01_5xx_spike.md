초안: PM 검토 필요
5xx 오류 비율 급증 (규칙 R1)
증상
- 최근 60초 창에서 요청이 20건 이상이고 5xx 비율이 20%를 넘는다.
- 앱 로그의 error 필드에 예외 종류가 남는다.
자주 보는 원인
- /error 경로는 항상 RuntimeError로 500을 내는 장애 주입용 경로다. 부하 생성기(loadgen)의 --error-rate가 이 경로를 친다.
- /는 db 컨테이너에 방문 기록을 INSERT하므로, DB에 연결하지 못하면 /가 500을 낸다(연결 대기 3초).
- /healthz는 DB를 확인하지 않아 DB 문제가 있어도 200일 수 있다.
먼저 확인할 것
- 5xx가 /error에 몰렸는지 /에 몰렸는지 본다.
- docker compose ps로 db와 app 상태를, docker compose logs db로 db 로그를 본다.
