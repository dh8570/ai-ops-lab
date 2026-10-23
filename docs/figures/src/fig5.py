from lib import *
s = frame("교체 로드맵과 완료 기준", "흐름은 유지하고 부품만 교체. 모든 교체는 v0 완료 기준을 회귀 테스트로 다시 통과해야 머지", 5,
          legend=[("box", None, "v0 스켈레톤"), ("solid", C["line"], "교체 단계")])
x0, wk = 470, 57.5   # week 1 starts at x0
s.rect(60, 215, 1800, 560, fill=C["panel"], stroke=C["border"], rx=12)
for w in range(1, 25):
    x = x0 + (w - 1) * wk
    s.add(f'<line x1="{x}" y1="262" x2="{x}" y2="752" stroke="{C["grid"]}" stroke-width="1"/>')
    s.text(x + wk / 2, 252, f"{w}", 14, 400, C["muted"], "middle", mono=True)
s.text(90, 252, "주차", 15, 600, C["muted"])
rows = [("P1", "스켈레톤 v0", "앱 · 로그 · 임계값 · LLM · Slack", 1, 3, True),
        ("2-1", "로그 수집", "JSON 파일 → ELK", 4, 6, False),
        ("2-2", "VM 구성", "수동 → Ansible", 7, 8, False),
        ("2-3", "메트릭", "→ Prometheus · Grafana", 9, 10, False),
        ("2-4", "이상 탐지", "임계값 → Isolation Forest", 11, 12, False),
        ("2-5", "지식 활용", "프롬프트 → RAG · pgvector", 13, 15, False),
        ("2-6", "배치", "→ Airflow 리포트", 16, 17, False),
        ("2-7", "에이전트", "→ Tool calling · n8n", 18, 18, False),
        ("2-8", "배포", "→ Terraform · OCI · Actions", 19, 21, False),
        ("P3", "시연 · 포트폴리오", "장애 시나리오 3종 데모", 22, 24, True)]
y = 290
for rid, name, desc, a, b, key in rows:
    s.text(90, y + 22, rid, 15, 700, C["blue"] if key else C["muted"], mono=True)
    s.text(150, y + 22, name, 17, 700, C["ink"])
    bx = x0 + (a - 1) * wk + 3; bw = (b - a + 1) * wk - 6
    s.rect(bx, y + 2, bw, 30, fill=C["bluet"] if key else "#EEF0F3", stroke=C["blue"] if key else C["line"], rx=6, sw=2 if key else 1.2)
    tx = bx + bw + 12
    if tx + len(desc) * 13 > 1840: s.text(bx - 12, y + 23, desc, 15, 400, C["body"], "end")
    else: s.text(tx, y + 23, desc, 15, 400, C["body"])
    y += 46
for w, lab in [(3, "v0 DoD"), (24, "완료")]:
    x = x0 + w * wk
    s.add(f'<path d="M{x},{252+6} l9,9 l-9,9 l-9,-9 z" fill="{C["orange"]}"/>' if False else "")
# DoD + limits
s.rect(60, 795, 1060, 215, fill=C["panel"], stroke=C["border"], rx=12)
s.text(90, 834, "v0 완료 기준 (Definition of Done) · 회귀 게이트", 19, 700, C["ink"])
dod = ["/error 주입 후 2분 이내 [FIRING] 알림 도착 (요약·원인 후보는 3주차)",
       "같은 장애에 대한 FIRING 알림 정확히 1건 (그룹핑)",
       "Ollama 중지 상태에서도 기본 템플릿 알림 전송 (유실 0건)",
       "정상 5분 유지 후 [RESOLVED] 알림과 지속 시간 전송"]
for i, t in enumerate(dod):
    yy = 874 + i * 34
    s.rect(92, yy - 16, 18, 18, fill="#FFFFFF", stroke=C["blue"], rx=4, sw=2)
    s.text(124, yy, t, 16, 400, C["body"])
    if i in (0, 1, 3):                                   # 알림 항목은 scripts/acceptance/alerts.py로 자동 검증 (2026-10-22 PASS)
        s.text(1100, yy, "자동 검증 PASS · alerts.py", 14, 600, C["green"], "end", mono=False)
s.rect(1140, 795, 720, 215, fill=C["panel"], stroke=C["border"], rx=12)
s.add(f'<rect x="1140" y="795" width="6" height="215" rx="3" fill="{C["orange"]}"/>')
s.text(1170, 834, "v0 알려진 한계", 19, 700, C["ink"])
lim = ["Watcher 자체 장애 미감지 → heartbeat 도입 예정", "자동 복구 없음, 조치는 사람이 결정", "단일 VM · 단일 앱 대상, HA 없음", "고정 임계값 → 트래픽 패턴 변화에 둔감 (2-4에서 보완)"]
s.lines(1170, 874, ["– " + t for t in lim], 16, 2.1, col=C["body"])
render(s, "fig5_roadmap")
