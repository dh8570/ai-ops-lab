import os
from html import escape

VERSION = "v0.2 · 2026-10-22"          # 그림 버전은 여기서만 바꾼다
W, H = 1920, 1080
SANS = "'Noto Sans CJK KR', sans-serif"
MONO = "'DejaVu Sans Mono', monospace"
C = dict(bg="#F6F7F9", panel="#FFFFFF", border="#D9DEE5", ink="#111827", body="#374151",
         muted="#6B7280", line="#9CA3AF", blue="#2563EB", bluet="#EEF3FF", orange="#C2410C",
         oranget="#FFF4EB", green="#047857", greent="#ECFDF5", red="#B91C1C", redt="#FEF2F2", grid="#E5E7EB")

class S:
    def __init__(s): s.o = []
    def add(s, x): s.o.append(x); return s
    def text(s, x, y, t, size=18, w=400, col=None, anchor="start", mono=False, ls=0):
        col = col or C["body"]
        s.add(f'<text x="{x}" y="{y}" font-family="{MONO if mono else SANS}" font-size="{size}" font-weight="{w}" fill="{col}" text-anchor="{anchor}" letter-spacing="{ls}">{escape(t)}</text>')
    def lines(s, x, y, ts, size=18, lh=1.45, **k):
        for i, t in enumerate(ts): s.text(x, y + i * size * lh, t, size, **k)
    def rect(s, x, y, w, h, fill=None, stroke=None, rx=10, sw=1.5, dash=None):
        fill = fill or C["panel"]; stroke = stroke or C["border"]
        d = f' stroke-dasharray="{dash}"' if dash else ""
        s.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')
    def path(s, d, col=None, sw=2, dash=None, end=True, start=False, fill="none"):
        col = col or C["line"]; mk = {C["blue"]: "b", C["line"]: "g", C["orange"]: "o", C["muted"]: "m", C["ink"]: "k"}.get(col, "g")
        a = f' stroke-dasharray="{dash}"' if dash else ""
        if end: a += f' marker-end="url(#a{mk})"'
        if start: a += f' marker-start="url(#a{mk})"'
        s.add(f'<path d="{d}" fill="{fill}" stroke="{col}" stroke-width="{sw}"{a}/>')
    def cyl(s, x, y, w, h, fill=None, stroke=None):
        fill = fill or C["panel"]; stroke = stroke or C["line"]; e = 10
        s.add(f'<path d="M{x},{y+e} A{w/2},{e} 0 0 1 {x+w},{y+e} V{y+h-e} A{w/2},{e} 0 0 1 {x},{y+h-e} Z" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        s.add(f'<path d="M{x},{y+e} A{w/2},{e} 0 0 0 {x+w},{y+e}" fill="none" stroke="{stroke}" stroke-width="1.5"/>')
    def label_bg(s, x, y, t, size=15, col=None, anchor="middle", mono=True, pad=6, fill=None):
        wch = sum((0.62 if mono else 0.55) * size if ord(c) < 128 else size * 1.0 for c in t)
        x0 = x - wch / 2 if anchor == "middle" else (x if anchor == "start" else x - wch)
        fb = fill or C["bg"]; s.rect(x0 - pad, y - size - 2, wch + 2 * pad, size + 10, fill=fb, stroke=fb, rx=4, sw=0)
        s.text(x, y, t, size, col=col or C["muted"], anchor=anchor, mono=mono)

def frame(title, sub, fig, legend=None, ver=None):
    ver = ver or VERSION
    s = S()
    defs = "".join(f'<marker id="a{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{v}"/></marker>'
                   for k, v in dict(b=C["blue"], g=C["line"], o=C["orange"], m=C["muted"], k=C["ink"]).items())
    s.add(f'<defs>{defs}</defs><rect width="{W}" height="{H}" fill="{C["bg"]}"/>')
    s.add(f'<rect width="{W}" height="6" fill="{C["blue"]}"/>')
    s.text(80, 66, "AI-OPS-LAB  /  DESIGN DOC", 17, 600, C["blue"], mono=True, ls=1)
    s.text(80, 120, title, 42, 700, C["ink"])
    s.text(80, 160, sub, 21, 400, C["muted"])
    s.text(1840, 66, f"{ver} · owner: dh8570", 16, 400, C["muted"], "end", mono=True)
    s.text(1840, 96, f"Fig. {fig} / 5", 16, 600, C["ink"], "end", mono=True)
    s.add(f'<line x1="80" y1="190" x2="1840" y2="190" stroke="{C["border"]}" stroke-width="1.5"/>')
    s.add(f'<line x1="80" y1="1028" x2="1840" y2="1028" stroke="{C["border"]}" stroke-width="1.5"/>')
    s.text(1840, 1058, "LLM 기반 장애 분석 어시스턴트", 16, 400, C["muted"], "end")
    if legend:
        x = 80
        for kind, col, lab in legend:
            if kind == "solid": s.path(f"M{x},1052 H{x+40}", col, 2)
            elif kind == "dash": s.path(f"M{x},1052 H{x+40}", col, 2, "6 5")
            elif kind == "box": s.rect(x, 1042, 40, 20, fill=C["bluet"], stroke=C["blue"], rx=4, sw=2.5)
            elif kind == "cyl": s.cyl(x + 8, 1040, 24, 24)
            s.text(x + 52, 1058, lab, 16, 400, C["body"]); x += 52 + len(lab) * 15 + 40
    return s

def render(s, name):
    """SVG를 PNG(3840x2160)로 변환하고 256색 팔레트로 압축해 FIG_OUT(기본: 현재 폴더)에 저장한다."""
    import io
    import cairosvg
    from PIL import Image
    out = os.environ.get("FIG_OUT", ".")
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">' + "".join(s.o) + "</svg>"
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=W * 2, output_height=H * 2)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
        os.path.join(out, f"{name}.png"), optimize=True)
    print(f"  {name}.png")
