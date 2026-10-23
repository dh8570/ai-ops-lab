from lib import *
s = frame("장애 상태 모델과 탐지 규칙", "장애 단위는 service, 켜진 규칙은 증거로 누적. 알림은 상태 전이에서만 발생", 4, legend=[("solid", C["ink"], "상태 전이"), ("solid", C["orange"], "Slack 알림 발생 (FIRING · UPDATE · RESOLVED)")])
s.rect(60, 215, 1010, 795, fill=C["panel"], stroke=C["border"], rx=12)
s.text(90, 258, "상태 전이 (incident lifecycle)", 20, 700, C["ink"])
def st(x, y, name, sub, col, fill):
    s.rect(x, y, 250, 96, fill=fill, stroke=col, rx=48, sw=2.5)
    s.text(x + 125, y + 44, name, 22, 700, C["ink"], "middle", mono=True)
    s.text(x + 125, y + 72, sub, 15, 400, C["body"], "middle")
st(120, 560, "NORMAL", "열린 장애 없음", C["green"], C["greent"])
st(660, 360, "FIRING", "장애 진행 중", C["red"], C["redt"])
st(660, 800, "RECOVERING", "규칙 미충족, 관찰 중", C["blue"], C["bluet"])
s.add(f'<circle cx="80" cy="608" r="9" fill="{C["ink"]}"/>'); s.path("M89,608 H118", C["ink"])
K, O = C["ink"], C["orange"]
s.path("M370,585 L658,420", O, 2.5)
s.label_bg(515, 470, "규칙 충족", 16, C["ink"], mono=False, fill="#FFFFFF"); s.label_bg(515, 494, "→ [FIRING]", 15, C["orange"], mono=False, fill="#FFFFFF")
s.path("M740,360 C740,270 870,270 870,360", K)
s.text(805, 236, "규칙 계속 충족 → 알림 억제", 15, 600, C["body"], "middle")
s.text(805, 258, "새 규칙 추가(escalate) → 장애 시작 180초 경과 시 [UPDATE]", 15, 600, C["orange"], "middle")
s.path("M845,456 V798", K); s.text(860, 620, "규칙 미충족", 16, 600, C["ink"])
s.path("M720,800 V458", K); s.text(705, 610, "다시 충족", 16, 600, C["ink"], "end"); s.text(705, 634, "(같은 장애 유지)", 15, 400, C["muted"], "end")
s.path("M660,850 L372,640", O, 2.5)
s.label_bg(470, 772, "5분 연속 정상", 16, C["ink"], mono=False, fill="#FFFFFF"); s.label_bg(470, 796, "→ [RESOLVED] + 장애 닫기", 15, C["orange"], mono=False, fill="#FFFFFF")
s.text(90, 960, "알림: FIRING(장애 시작) · UPDATE(유예 후 새 규칙) · RESOLVED(장애 종료) 세 경우만", 15, 400, C["muted"])
s.text(90, 984, "R4는 15초 프로브가 로그 창을 기다리지 않고 즉시 장애를 연다 · 플래핑은 RECOVERING 구간이 흡수", 15, 400, C["muted"])

# right: rule spec
x0 = 1100; s.rect(x0, 215, 760, 795, fill=C["panel"], stroke=C["border"], rx=12)
s.text(x0 + 30, 258, "탐지 규칙 (초기값)", 20, 700, C["ink"])
cols = [x0 + 30, x0 + 110, x0 + 590]
s.rect(x0 + 20, 278, 720, 40, fill="#F3F4F6", stroke="#F3F4F6", rx=6)
for x, h in zip(cols, ["ID", "조건  (R1~R3: 60초 창, R4: 15초 프로브)", "심각도"]): s.text(x, 304, h, 15, 700, C["muted"])
rows = [("R1", ["requests ≥ 20  AND", "5xx / requests > 20%"], "critical", C["red"]),
        ("R2", ["requests ≥ 20  AND", "p95 latency > 2,000 ms"], "warning", C["orange"]),
        ("R3", ["rpm < 0.7 × baseline", "AND baseline ≥ 60 rpm (최소 요청 조건 없음)"], "warning", C["orange"]),
        ("R4", ["probe GET /healthz (15초 주기)", "timeout 2s, 2회 연속 실패"], "critical", C["red"])]
y = 346
for rid, cond, sev, col in rows:
    s.text(cols[0], y, rid, 18, 700, C["ink"], mono=True)
    s.text(cols[1], y, cond[0], 17, col=C["ink"], mono=not any(ord(ch) > 127 and ch not in "≥×" for ch in cond[0]))
    s.text(cols[1], y + 25, cond[1], 17, col=C["ink"], mono=not any(ord(ch) > 127 and ch not in "≥×" for ch in cond[1]))
    s.rect(cols[2], y - 20, 110, 30, fill="#FFFFFF", stroke=col, rx=15, sw=1.5); s.text(cols[2] + 55, y + 1, sev, 15, 600, col, "middle", mono=True)
    s.add(f'<line x1="{x0+20}" y1="{y+40}" x2="{x0+740}" y2="{y+40}" stroke="{C["grid"]}"/>'); y += 64
s.text(x0 + 30, 638, "운영 파라미터", 20, 700, C["ink"])
kv = [("장애 단위", "service (규칙은 한 장애에 누적)"), ("평가 주기", "로그 60초 창 · 프로브 15초"), ("기준선", "정상 창 rpm 중앙값 (3~10개 창)"),
      ("UPDATE 유예", "장애 시작 후 180초 (시간 기준)"), ("복구 조건", "5개 창(5분) 연속 규칙 미충족"),
      ("LLM (3주차)", "마스킹 → 최근 50줄 + 런북, 30초, 실패 시 기본 템플릿"), ("상태 저장", "SQLite 체크포인트 (읽기 위치·기준선)")]
y = 676
for k, v in kv:
    s.text(x0 + 30, y, k, 16, 600, C["muted"]); s.text(x0 + 220, y, v, 16, 400, C["ink"])
    s.add(f'<line x1="{x0+20}" y1="{y+16}" x2="{x0+740}" y2="{y+16}" stroke="{C["grid"]}"/>'); y += 42
s.text(x0 + 30, 980, "모든 기준값은 초기값이며, 장애 주입 실험으로 탐지율·오탐률을 보고 조정", 15, 400, C["muted"])
render(s, "fig4_state_rules")
