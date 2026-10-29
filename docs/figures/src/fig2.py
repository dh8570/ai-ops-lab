from lib import *
s = frame("시스템 아키텍처 (v0.3)", "GPU 서버 위 VM 하나에 Compose 서비스. LLM은 GPU 서버의 Ollama를 VM↔서버 통로로 호출. 외부 egress는 Slack 웹훅 하나", 2,
          legend=[("solid", C["blue"], "동기 호출 (HTTP / SQL)"), ("dash", C["line"], "파일 I/O (볼륨)"),
                  ("solid", C["orange"], "외부 egress"), ("box", None, "오케스트레이터"), ("cyl", None, "저장소 / 볼륨")],
          ver="v0.3 · 2026-10-27")                       # Fig 2·5만 v0.3 (lib.VERSION은 v0.2로 둔다)
# boundaries
s.rect(60, 215, 1440, 795, fill="#FBFBFC", stroke=C["line"], rx=14, dash="8 6")
s.text(84, 248, "HOST  controller1 · Ubuntu 26.04 · GPU RTX 4080 16GB (드라이버 595-open)", 17, 600, C["muted"], mono=False)
# ollama: VM 바깥, 서버에 직접 (systemd 서비스)
s.rect(90, 262, 1380, 84, fill=C["panel"], stroke=C["line"], rx=10, sw=1.5)
s.text(106, 296, "ollama", 21, 700, C["ink"])
s.text(206, 296, "systemd 서비스 · 10.97.0.1:11434 (통로 주소에만 공개)", 16, 400, C["muted"])   # 한글이 있으면 mono 금지(DejaVu Mono에 한글 글리프 없음)
s.text(106, 326, "Ollama 0.40.2 · qwen2.5:7b (Q4_K_M)", 15, 400, C["body"])
# VM
s.rect(90, 362, 1380, 623, fill=C["panel"], stroke=C["ink"], rx=12, sw=1.5)
s.text(114, 392, "MULTIPASS VM  ai-lab-gpu · Ubuntu 24.04 · 8 vCPU / 32 GB / 100 GB", 17, 600, C["ink"])
s.rect(120, 410, 1320, 558, fill="#F9FAFB", stroke=C["border"], rx=10, dash="4 4")
s.text(144, 438, "docker compose · network: ai-ops-lab_default", 16, 400, C["muted"], mono=True)

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

c = [160, 500, 840, 1180]; r = [450, 610, 770]
box(c[0], r[0], "traffic", "python:3.12-slim", ["상시 배경 트래픽 5 rps"], h=100)
box(c[0], r[1] - 20, "loadgen", "python:3.12-slim", ["장애 주입 · 필요할 때만"], h=100)
box(c[1], r[0], "app", "Flask · :8000", ["JSON 로그 출력", "장애 주입 엔드포인트"])
box(c[2], r[0], "db", "postgres:16 · :5432", ["volume: db-data", "compose 내부 전용"])
vol(c[1], r[1], "app-logs", "volume · app.jsonl", "app: rw / watcher: ro")
box(c[2], r[1], "watcher", "python:3.12-slim", ["집계·판정·프로브·그룹핑", "알림 전송"], accent=True)
# runbooks: 아직 없음 -> 점선 테두리
s.rect(c[1], r[2], 230, 118, fill=C["panel"], stroke=C["line"], rx=10, sw=1.5, dash="6 5")
s.text(c[1] + 16, r[2] + 44, "runbooks", 19, 700, C["ink"])
s.text(c[1] + 16, r[2] + 70, "bind mount (ro)", 15, 400, C["muted"], mono=True)
s.text(c[1] + 16, r[2] + 94, "A주 작성 예정", 15, 400, C["body"])
vol(c[2], r[2], "watcher-state", "volume · SQLite", "장애 상태, 체크포인트")

B, G, O = C["blue"], C["line"], C["orange"]
# traffic / loadgen -> app
s.path(f"M{c[0]+230},{r[0]+45} H{c[1]}", B); s.label_bg((c[0]+230+c[1])/2, r[0]+33, "HTTP")
s.path(f"M{c[0]+230},{r[1]+30} H{c[1]-45} V{r[0]+90} H{c[1]}", B)
# app -> db
s.path(f"M{c[1]+230},{r[0]+60} H{c[2]}", B); s.label_bg((c[1]+230+c[2])/2, r[0]+48, "SQL")
s.path(f"M{c[1]+115},{r[0]+118} V{r[1]}", G, dash="6 5"); s.text(c[1]+127, r[0]+144, "append", 15, 400, C["muted"], mono=True)
s.path(f"M{c[1]+230},{r[1]+50} H{c[2]}", G, dash="6 5"); s.label_bg((c[1]+230+c[2])/2, r[1]+38, "tail")
s.path(f"M{c[1]+230},{r[2]+60} H{c[1]+285} V{r[1]+100} H{c[2]}", G, dash="6 5"); s.text(c[1]+295, r[1]+160, "read", 15, 400, C["muted"], mono=True)
s.path(f"M{c[2]+115},{r[1]+128} V{r[2]}", G, dash="6 5", start=True); s.text(c[2]+127, r[1]+144, "r/w", 15, 400, C["muted"], mono=True)
# probe: watcher -> app
s.path(f"M{c[2]},{r[1]+15} H790 V{r[0]+90} H{c[1]+230}", B)
s.text(797, r[0]+130, "probe", 15, 400, C["blue"], mono=True)
# watcher -> ollama (VM 경계를 넘어 서버로). 아직 Watcher가 Ollama를 부르지 않으므로 점선 (A주 구현 예정)
s.path(f"M{c[2]+230},{r[1]+30} H1200 V346", B, dash="6 5")
s.text(1212, 500, "HTTP 10.97.0.1:11434", 15, 600, C["blue"], mono=True)
s.lines(1212, 524, ["A주 구현 예정", "VM↔서버 통로", "감시 루프 밖에서 호출"], 15, 1.5, col=C["body"])
# egress
s.path(f"M{c[2]+230},{r[1]+90} H1560", O, 2.5)
s.text(1100, r[1]+114, "HTTPS 443 · webhook", 16, 600, C["orange"], mono=True)
s.text(1560, 248, "EXTERNAL", 17, 600, C["muted"], mono=True)
s.rect(1560, 640, 300, 118, fill=C["oranget"], stroke=C["orange"], rx=10, sw=2)
s.text(1576, 674, "Slack", 21, 700, C["ink"]); s.text(1576, 702, "incoming webhook (#alerts)", 15, 400, C["muted"], mono=True)
s.text(1576, 728, "FIRING·UPDATE·RESOLVED", 15, 400, C["body"], mono=True)
# access
s.rect(1560, 790, 300, 200, fill=C["panel"], stroke=C["border"], rx=10)
s.text(1576, 824, "운영자 접근", 19, 700, C["ink"])
s.lines(1576, 856, ["Windows", "VS Code Remote-SSH + Claude Code"], 15, 1.5, col=C["body"])
s.text(1576, 910, "via Tailscale + OpenSSH(키 인증)", 15, 400, C["muted"])
s.lines(1576, 932, ["ai-lab-gpu :22", "app :8000  tailnet only"], 15, 1.5, col=C["muted"], mono=True)
s.path("M1560,900 H1472", B, dash="6 5")
# exposure note
s.text(144, 928, "노출 정책: app :8000은 tailnet 주소에만 · db :5432는 compose 내부 · ollama :11434는 VM↔서버 통로 주소에만", 16, 400, C["body"])
s.text(144, 954, "서버에는 Tailscale 없음 · 공인 포트 개방 없음", 16, 400, C["body"])
render(s, "fig2_architecture")
