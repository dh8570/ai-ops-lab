from lib import *
s = frame("장애 감지 → 알림 시퀀스", "15초 틱 루프: 매 틱 R4 프로브, 4번째 틱(60초)마다 로그 창 판정. 알림은 상태 전이에서만 발생", 3,
          legend=[("solid", C["blue"], "요청"), ("dash", C["line"], "응답 / 파일 읽기·쓰기 / 3주차 예정"), ("solid", C["orange"], "외부 알림")])
P = [("loadgen", 170), ("app", 420), ("app-logs", 670), ("watcher", 920), ("watcher-state", 1170), ("ollama", 1420), ("Slack", 1700)]
X = {n: x for n, x in P}
for n, x in P:
    acc = n == "watcher"; ext = n == "Slack"
    s.rect(x - 95, 212, 190, 50, fill=C["bluet"] if acc else (C["oranget"] if ext else C["panel"]),
           stroke=C["blue"] if acc else (C["orange"] if ext else C["line"]), rx=8, sw=2.5 if acc else 1.5)
    s.text(x, 244, n, 18, 700, C["ink"], "middle", mono=True)
    s.add(f'<line x1="{x}" y1="262" x2="{x}" y2="1010" stroke="{C["line"]}" stroke-width="1.2" stroke-dasharray="4 5"/>')

def msg(a, b, y, t, col=None, dash=None, n=None):
    col = col or C["blue"]; xa, xb = X[a], X[b]
    s.path(f"M{xa},{y} H{xb}", col, 2, dash)
    lx = min(xa, xb) + 12
    lab = (f"{n}  " if n else "") + t
    s.text(lx, y - 9, lab, 15, 400 if dash else 600, C["muted"] if dash else C["ink"])
def self_(a, y, t, n=None, h=26):
    x = X[a]; s.path(f"M{x},{y} H{x+40} V{y+h} H{x+4}", C["blue"], 2)
    s.text(x + 50, y + 18, (f"{n}  " if n else "") + t, 15, 600, C["ink"])
def frame_(x1, y1, x2, y2, tag, cond):
    s.rect(x1, y1, x2 - x1, y2 - y1, fill="none", stroke=C["ink"], rx=4, sw=1.2)
    s.add(f'<path d="M{x1},{y1} h{len(tag)*11+20} v20 l-10,10 H{x1} Z" fill="{C["ink"]}"/>')
    s.text(x1 + 8, y1 + 21, tag, 15, 700, "#FFFFFF", mono=True)
    s.text(x1 + len(tag) * 11 + 34, y1 + 21, cond, 15, 600, C["body"])
def divider(y, x1, x2, label):
    s.add(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{C["ink"]}" stroke-width="1.2" stroke-dasharray="8 5"/>')
    s.text(x1 + 12, y + 20, label, 15, 600, C["body"])

msg("loadgen", "app", 305, "GET /error  (반복)", n="1")
msg("app", "app-logs", 345, "append  {ts, path, status, latency_ms}", C["line"], "6 5", "2")

frame_(380, 372, 1860, 1008, "loop", "[매 15초 틱]")
msg("watcher", "app", 430, "GET /healthz  (R4 프로브, timeout 2s)", n="3")
msg("app", "watcher", 458, "200 / timeout", C["line"], "6 5")

frame_(560, 474, 1840, 598, "opt", "[R4 막 켜짐 · 열린 장애 없음]  → 로그 창을 기다리지 않고 즉시 개시")
msg("watcher", "watcher-state", 534, "insert incident  FIRING  (trigger R4)", n="4")
msg("watcher", "Slack", 580, "POST webhook  [FIRING]", C["orange"], n="5")

frame_(560, 612, 1840, 1002, "opt", "[4번째 틱 = 60초 로그 창]")
msg("watcher", "app-logs", 664, "read new lines (offset)", n="6")
msg("app-logs", "watcher", 692, "lines", C["line"], "6 5")
self_("watcher", 706, "집계 → R1·R2·R3 평가 + R4 창 플래그", "7")
msg("watcher", "watcher-state", 770, "step(켜진 규칙)  상태 전이", n="8")
msg("watcher-state", "watcher", 796, "action: fire | escalate | suppress | recovering | resolve", C["line"], "6 5")

frame_(580, 810, 1830, 996, "alt", "[fire · 신규 장애]")
msg("watcher", "ollama", 860, "마스킹 + POST /api/generate  (3주차, timeout 30s)", C["line"], "6 5", "9")
msg("watcher", "Slack", 888, "POST webhook  [FIRING]", C["orange"], n="10")
divider(906, 580, 1830, "[escalate · 장애 시작 후 180초 경과]  새 규칙 추가")
msg("watcher", "Slack", 954, "POST webhook  [UPDATE]  (새로 추가된 신호)", C["orange"], n="11")
divider(962, 580, 1830, "[그 외: 억제 · refire · 유예 안의 escalate]  알림 없음, 규칙만 누적")

s.rect(80, 600, 286, 196, fill=C["panel"], stroke=C["border"], rx=10)
s.text(100, 636, "복구 경로 (60초 창 기준)", 18, 700, C["ink"])
s.lines(100, 672, ["5개 창 연속 규칙 미충족", "→ RECOVERING → RESOLVED", "→ Slack [RESOLVED]", "    + 지속 시간, 장애 창 수", "RECOVERING 중 재발 = 같은 장애"], 15, 1.6, col=C["body"])
s.rect(80, 820, 286, 150, fill=C["panel"], stroke=C["border"], rx=10)
s.text(100, 856, "탐지 지연 (실측)", 18, 700, C["ink"])
s.lines(100, 890, ["로그 규칙 R1~R3: 최대 약 2분", "R4 프로브: 약 30초", "(15초 × 2회 연속 실패)"], 15, 1.6, col=C["body"])
render(s, "fig3_sequence")
