from lib import *
s = frame("시스템 아키텍처 (v0)", "단일 VM, Docker Compose 컨테이너 5개. Watcher가 오케스트레이터이며 외부 egress는 Slack 웹훅 하나", 2, legend=[("solid", C["blue"], "동기 호출 (HTTP / SQL)"), ("dash", C["line"], "파일 I/O (볼륨)"),
           ("solid", C["orange"], "외부 egress"), ("box", None, "오케스트레이터"), ("cyl", None, "저장소 / 볼륨")])
# boundaries
s.rect(60, 215, 1440, 795, fill="#FBFBFC", stroke=C["line"], rx=14, dash="8 6")
s.text(84, 248, "HOST  jn-controller1 · Ubuntu 26.04 (업무 서버, 서비스 컨테이너 공존)", 17, 600, C["muted"], mono=False)
s.rect(90, 265, 1380, 725, fill=C["panel"], stroke=C["ink"], rx=12, sw=1.5)
s.text(114, 298, "MULTIPASS VM  ai-lab · Ubuntu 24.04 · 8 vCPU / 32 GB / 150 GB", 17, 600, C["ink"])
s.rect(120, 318, 1320, 640, fill="#F9FAFB", stroke=C["border"], rx=10, dash="4 4")
s.text(144, 348, "docker compose · network: ai-ops-lab_default", 16, 400, C["muted"], mono=True)

def box(x, y, name, img, note, accent=False, w=230, h=118):
    s.rect(x, y, w, h, fill=C["bluet"] if accent else C["panel"], stroke=C["blue"] if accent else C["line"], rx=10, sw=3 if accent else 1.5)
    s.text(x + 16, y + 34, name, 21, 700, C["ink"])
    s.text(x + 16, y + 62, img, 15, 400, C["muted"], mono=True)
    for i, n in enumerate(note): s.text(x + 16, y + 88 + i * 22, n, 15, 400, C["body"])
def vol(x, y, name, kind, note, w=230, h=118):
    s.cyl(x, y, w, h)
    s.text(x + 16, y + 44, name, 19, 700, C["ink"])
    s.text(x + 16, y + 70, kind, 15, 400, C["muted"], mono=True)
    s.text(x + 16, y + 94, note, 15, 400, C["body"])

c = [160, 500, 840, 1180]; r = [380, 590, 800]
box(c[0], r[0], "loadgen", "python:3.12-slim", ["정상 + /error, /slow 트래픽"])
box(c[1], r[0], "app", "Flask · :8000", ["JSON 로그 출력", "장애 주입 엔드포인트"])
box(c[2], r[0], "db", "postgres:16 · :5432", ["volume: db-data", "내부 전용"])
vol(c[1], r[1], "app-logs", "volume · app.jsonl", "app: rw / watcher: ro")
box(c[2], r[1], "watcher", "python:3.12-slim", ["집계·판정·프로브·그룹핑", "마스킹·LLM 호출·전송"], accent=True)
box(c[3], r[1], "ollama", "ollama/ollama · :11434", ["8B 이하 로컬 모델", "volume: ollama-models"])
vol(c[1], r[2], "runbooks", "bind mount (ro)", "./llm/runbooks/*.md")
vol(c[2], r[2], "watcher-state", "volume · SQLite", "장애 상태, 체크포인트")

B, G, O = C["blue"], C["line"], C["orange"]
s.path(f"M{c[0]+230},440 H{c[1]}", B); s.label_bg((c[0]+230+c[1])/2, 428, "HTTP")
s.path(f"M{c[1]+230},440 H{c[2]}", B); s.label_bg((c[1]+230+c[2])/2, 428, "SQL")
s.path(f"M{c[1]+115},498 V{r[1]}", G, dash="6 5"); s.text(c[1]+127, 552, "append", 15, 400, C["muted"], mono=True)
s.path(f"M{c[1]+230},640 H{c[2]}", G, dash="6 5"); s.label_bg((c[1]+230+c[2])/2, 628, "tail")
s.path(f"M{c[1]+230},860 H{c[1]+285} V690 H{c[2]}", G, dash="6 5"); s.text(c[1]+295, 780, "read", 15, 400, C["muted"], mono=True)
s.path(f"M{c[2]+115},708 V{r[2]}", G, dash="6 5", start=True); s.text(c[2]+127, 760, "r/w", 15, 400, C["muted"], mono=True)
s.path(f"M{c[2]+230},650 H{c[3]}", B, start=True); s.label_bg((c[2]+230+c[3])/2, 636, ":11434")
s.text((c[2]+230+c[3])/2, 678, "30s", 15, 400, C["muted"], "middle", mono=True)
s.path(f"M{c[2]},605 H790 V470 H{c[1]+230}", B)
s.text(797, 548, "probe", 15, 400, C["blue"], mono=True)
# egress
s.path(f"M{c[2]+115},{r[1]} V545 H1710 V590", O, 2.5)
s.text(1530, 534, "HTTPS 443 · webhook", 16, 600, C["orange"], mono=True)
s.text(1560, 248, "EXTERNAL", 17, 600, C["muted"], mono=True)
s.rect(1560, 590, 300, 118, fill=C["oranget"], stroke=C["orange"], rx=10, sw=2)
s.text(1576, 624, "Slack", 21, 700, C["ink"]); s.text(1576, 652, "incoming webhook (#alerts)", 15, 400, C["muted"], mono=True)
s.text(1576, 678, "FIRING·UPDATE·RESOLVED", 15, 400, C["body"], mono=True)
# access
s.rect(1560, 770, 300, 220, fill=C["panel"], stroke=C["border"], rx=10)
s.text(1576, 804, "운영자 접근", 19, 700, C["ink"])
s.lines(1576, 836, ["Windows · VS Code / Cursor", "iPhone · Termius"], 15, 1.5, col=C["body"])
s.lines(1576, 892, ["via Tailscale (WireGuard)", "ai-lab :22  OpenSSH (키 인증)", "app :8000  tailnet only"], 15, 1.5, col=C["muted"], mono=True)
s.path("M1560,880 H1472", B, dash="6 5")
# exposure note
s.text(144, 940, "노출 정책: app :8000만 tailnet IP에 바인딩 · db :5432, ollama :11434는 compose 내부 전용 · 공인 포트 개방 없음", 16, 400, C["body"])
render(s, "fig2_architecture")
