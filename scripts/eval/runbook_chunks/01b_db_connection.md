# 5xx 급증: DB 연결 실패
자주 보는 원인
- /는 db 컨테이너에 방문 기록을 INSERT하므로, DB에 연결하지 못하면 /가 500을 낸다(연결 대기 3초).
- /healthz는 DB를 확인하지 않아 DB 문제가 있어도 200일 수 있다.
먼저 확인할 것
- docker compose ps로 db와 app 상태를, docker compose logs db로 db 로그를 본다.
