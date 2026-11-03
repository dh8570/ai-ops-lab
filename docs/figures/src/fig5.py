from lib import *
s = frame("4주 AI 우선 로드맵과 완료 기준", "AI를 어떤 문제에 적용하고, 어떻게 동작하고, 데이터를 어떻게 쓰는지 배우는 4주. 매주 AI 질문 세 가지와 채점표·비교표로 끝낸다", 5,
          legend=[("box", None, "이번 주 (A)"), ("solid", C["line"], "완료 · 계획")],
          ver="v0.4 · 2026-11-02")                       # Fig 2는 v0.3, Fig 5는 v0.4 (lib.VERSION은 v0.2로 둔다)
x0, dw = 520, 33.8                                        # 가로축: 10/16부터 하루 dw px, 10/16~11/23 = 39일
def dx(d): return x0 + d * dw                             # d = 10/16 기준 경과일 (그 날의 시작 위치)
TODAY = 11                                                # 10/27 = 10/16 + 11일
s.rect(60, 215, 1800, 560, fill=C["panel"], stroke=C["border"], rx=12)
# 눈금: 시작(10/16), 직전 주(10/20), 각 주 경계(10/27·11/6·11/12·11/18), 끝(11/23)
for d, lab in [(0, "10/16"), (4, "10/20"), (11, "10/27"), (21, "11/6"), (27, "11/12"), (33, "11/18"), (39, "11/23")]:
    s.add(f'<line x1="{dx(d)}" y1="262" x2="{dx(d)}" y2="752" stroke="{C["grid"]}" stroke-width="1"/>')
    if d != TODAY: s.text(dx(d) + (4 if d == 39 else 0), 252, lab, 14, 400, C["muted"], "end" if d == 39 else "middle", mono=True)
s.text(90, 252, "단계", 15, 600, C["muted"])
# (표지, 주제, 결과물 줄들, 시작일, 끝일, 상태, 막대 안 글자). 날짜는 10/16 기준 경과일 [시작, 끝)
rows = [("완료", "기반", ["앱·로그·Watcher·알림·합격 시험", "+ GPU 서버 이전"], 0, 11, "done", "완료 · 10/16 ~ 10/26"),
        ("A", "LLM이 장애를 분석하게 하기", ["→ 장애 알림에 분석 붙임 · 합격 시험 추가", "+ 탐험 사이트 척추(M0) · 현미경 엔진(M2)"], 11, 21, "key", "10/27 ~ 11/5 (연장)"),
        ("B", "데이터 · RAG · 채점표", ["→ 채점표 5개 · RAG 비교표", "+ 저장 데이터 설명"], 21, 27, "plan", "11/6 ~ 11/11"),
        ("C", "규칙 대 ML 비교 (최소) · 현미경 화면", ["→ 규칙 대 ML 비교표 1개", "+ 현미경 화면 M3(임베딩·Prefill·첫 토큰)"], 27, 33, "plan", "11/12 ~ 11/17"),
        ("D", "마무리 · 공개 (정적 내보내기)", ["→ 공개 링크 · 최종 문서 · 시연", "현미경 M4·M5와 에이전트는 선택"], 33, 39, "plan", "11/18 ~ 11/23")]
y = 290
for tag, name, desc, a, b, st, inside in rows:
    s.text(90, y + 24, tag, 16, 700, {"done": C["green"], "key": C["blue"]}.get(st, C["muted"]), mono=(st != "done"))   # 한글은 mono 금지
    s.text(150, y + 24, name, 17, 700, C["ink"])
    for i, t in enumerate(desc): s.text(150, y + 48 + i * 20, t, 14, 400, C["green"] if st == "done" else C["body"])
    fill, stroke, sw, dash = {"done": ("#EEF0F3", C["line"], 1.2, None), "key": (C["bluet"], C["blue"], 2.5, None),
                              "plan": ("#FFFFFF", C["line"], 1.5, None)}[st]
    bx, bw = dx(a) + 3, (b - a) * dw - 6
    s.rect(bx, y + 6, bw, 36, fill=fill, stroke=stroke, rx=6, sw=sw, dash=dash)
    s.text(bx + bw / 2, y + 30, inside, 14, 600, C["ink"] if st == "key" else C["muted"], "middle", mono=(st in ("key", "plan") and inside.isascii()))
    y += 90
# A주 시작(10/27) 세로선
s.add(f'<line x1="{dx(TODAY)}" y1="262" x2="{dx(TODAY)}" y2="752" stroke="{C["orange"]}" stroke-width="2.5"/>')
s.text(dx(TODAY), 252, "A주 시작 10/27", 14, 700, C["orange"], "middle", mono=False)
# 완료 기준 + 범위 밖
s.rect(60, 795, 1060, 215, fill=C["panel"], stroke=C["border"], rx=12)
s.text(90, 834, "완료 기준 · 회귀 게이트", 19, 700, C["ink"])
dod = [("/error 주입 후 2분 이내 [FIRING] 알림 도착 (요약·원인 후보는 A주)", "자동 검증 PASS", C["green"]),
       ("같은 장애에 대한 FIRING 알림 정확히 1건 (그룹핑)", "자동 검증 PASS", C["green"]),
       ("Ollama 중지 상태에서도 기본 알림 전송 (유실 0건)", "A주에 자동 검증 추가", C["orange"]),
       ("정상 5분 유지 후 [RESOLVED] 알림과 지속 시간 전송", "자동 검증 PASS", C["green"])]
for i, (t, st, col) in enumerate(dod):
    yy = 874 + i * 34
    s.rect(92, yy - 16, 18, 18, fill="#FFFFFF", stroke=C["blue"], rx=4, sw=2)
    s.text(124, yy, t, 16, 400, C["body"])
    s.text(1100, yy, st, 14, 600, col, "end", mono=False)
s.rect(1140, 795, 720, 215, fill=C["panel"], stroke=C["border"], rx=12)
s.add(f'<rect x="1140" y="795" width="6" height="215" rx="3" fill="{C["orange"]}"/>')
s.text(1170, 834, "범위 밖 (프로젝트 이후)", 19, 700, C["ink"])
s.lines(1170, 874, ["ELK · Ansible · Prometheus · Airflow", "Terraform · OCI · GitHub Actions"], 16, 2.1, col=C["body"], mono=True)
s.text(1170, 966, "AI 단계가 요구할 때만 그 부분을 최소로", 16, 400, C["body"])
render(s, "fig5_roadmap")
