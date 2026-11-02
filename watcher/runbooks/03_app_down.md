초안: PM 검토 필요
앱 무응답 (규칙 R4, 프로브 연속 실패)
증상
- Watcher가 15초마다 /healthz를 부르는데 2초 안에 응답이 없는 일이 2번 연속 일어난다.
- 앱이 완전히 멈추면 로그에 요청이 없어 규칙 R3(처리량 급감)도 함께 켜진다.
자주 보는 원인
- app 컨테이너가 정지했거나 재시작 중이다(compose에 restart: unless-stopped).
- app은 시작할 때 DB에 접속하므로, db가 준비되지 않으면 app이 뜨지 못한다.
- 느린 요청이 워커 2개를 모두 잡아 /healthz도 응답하지 못한다(포화).
먼저 확인할 것
- docker compose ps로 app과 db가 실행 중인지 본다.
- docker compose logs app의 마지막 줄과, 정지 직전 요청이 느렸는지 본다.
