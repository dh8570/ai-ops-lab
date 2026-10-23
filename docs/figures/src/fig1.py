from lib import *
s = frame("LLM 기반 장애 분석 어시스턴트 · 개요", "장애 감지에서 끝나지 않고, 원인 후보와 확인 항목까지 담은 알림으로 초동 대응 시간을 줄인다", 1)
# problem / solution
s.rect(60, 215, 1040, 250, fill=C["panel"], stroke=C["border"], rx=12)
s.text(90, 256, "문제", 15, 700, C["muted"], mono=False, ls=2)
s.lines(90, 292, ["기존 알림은 “에러 발생”만 전달한다.", "담당자는 알림을 받은 뒤 로그 조회부터 다시 시작한다."], 20, 1.5, col=C["ink"])
s.add(f'<line x1="90" y1="358" x2="1070" y2="358" stroke="{C["grid"]}"/>')
s.text(90, 394, "접근", 15, 700, C["blue"], ls=2)
s.lines(90, 428, ["감지 직후 LLM이 로그와 런북을 읽고, 원인 후보 · 확인 항목을 알림에 함께 싣는다."], 20, 1.5, col=C["ink"])
# KPI targets
s.text(60, 512, "v0 목표 지표", 19, 700, C["ink"])
kpi = [("< 2분", "알림 도착 시간", "장애 주입 시점 기준"), ("1건", "FIRING 알림", "같은 장애 기준 (그룹핑)"), ("0건", "알림 유실", "LLM 장애 시에도 전송"), ("0원", "운영 비용", "로컬 LLM · 무료 인프라")]
for i, (big, lab, note) in enumerate(kpi):
    x = 60 + i * 266
    s.rect(x, 530, 248, 150, fill=C["panel"], stroke=C["border"], rx=12)
    s.text(x + 22, 594, big, 40, 700, C["blue"])
    s.text(x + 22, 632, lab, 18, 700, C["ink"]); s.text(x + 22, 660, note, 15, 400, C["muted"])
# scope
s.rect(60, 705, 515, 305, fill=C["panel"], stroke=C["border"], rx=12)
s.text(86, 744, "범위 안 (In scope)", 18, 700, C["green"])
s.lines(86, 784, ["– 로그 + 합성 프로브 탐지 (R1~R4)", "– LLM 요약 · 원인 후보 · 확인 항목", "– Slack FIRING · UPDATE · RESOLVED 알림", "– 장애 그룹핑(중복 억제), 민감정보 마스킹", "– LLM 실패 시 기본 알림 대체"], 16, 2.0, col=C["body"])
s.rect(585, 705, 515, 305, fill=C["panel"], stroke=C["border"], rx=12)
s.text(611, 744, "범위 밖 (Out of scope)", 18, 700, C["red"])
s.lines(611, 784, ["– 자동 복구 (판단과 조치는 사람)", "– 다중 서비스 · 분산 추적", "– 고가용성 (HA) 구성", "– 외부 LLM API 사용", "– Watcher 자체 감시 (heartbeat)"], 16, 2.0, col=C["body"])
# alert example
x0 = 1130
s.text(x0, 248, "알림 예시 (Slack, 목표 형태 · 샘플 데이터)", 18, 700, C["ink"])
s.rect(x0, 266, 730, 744, fill="#FFFFFF", stroke=C["border"], rx=12)
s.add(f'<rect x="{x0+24}" y="290" width="6" height="696" rx="3" fill="#E5484D"/>')
X = x0 + 50; t = "#1D1C1D"; m = "#616061"
s.text(X, 326, "ai-ops-bot  APP", 17, 700, "#1D1C1D"); s.text(X + 150, 326, "오후 2:03", 14, 400, m)
s.text(X, 368, "[FIRING] CRITICAL · visit-counter", 22, 700, "#1D1C1D")
s.text(X, 398, "R1 error_rate · incident #0007", 16, 400, m, mono=True)
fields = [("창", "14:01:00–14:02:00"), ("요청", "142"), ("5xx", "118 (83%)"), ("p95", "9 ms")]
for i, (k, v) in enumerate(fields):
    fx = X + (i % 2) * 320; fy = 444 + (i // 2) * 58
    s.text(fx, fy, k, 14, 700, m); s.text(fx, fy + 26, v, 17, 400, t, mono=True)
s.text(X, 580, "요약", 15, 700, m)
s.lines(X, 610, ["/ 경로 요청 대부분이 HTTP 500으로 실패했고, 같은 시각", "DB 이름 해석 실패(Temporary failure in name resolution)가 반복됩니다."], 17, 1.55, col=t)
s.text(X, 686, "원인 후보", 15, 700, m)
s.lines(X, 716, ["1. db 컨테이너 중지 (Docker DNS에서 이름이 제거됨)", "2. Compose 서비스 이름 또는 네트워크 설정 오류"], 17, 1.55, col=t)
s.text(X, 792, "먼저 확인할 것", 15, 700, m)
s.lines(X, 822, ["docker compose ps db", "docker compose logs --since 5m db", "runbook: db-connection.md"], 16, 1.6, col="#1264A3", mono=True)
s.add(f'<line x1="{X}" y1="912" x2="{x0+700}" y2="912" stroke="#E5E7EB"/>')
s.lines(X, 946, ["AI 요약은 참고용이며 판단과 조치는 담당자가 합니다.", "새 원인이 늦게 붙으면 [UPDATE], 복구되면 [RESOLVED]로 닫습니다."], 15, 1.6, col=m)
render(s, "fig1_overview")
