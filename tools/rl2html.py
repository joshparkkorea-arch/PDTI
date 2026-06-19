# -*- coding: utf-8 -*-
"""
rl2html.py — 기존 뉴스레터 PDF 빌드 스크립트를 'Invest Story' HTML 기사로 변환.
ReportLab 기본요소(Paragraph/Table/Spacer 등)를 가짜 모듈로 바꿔치기 한 뒤 스크립트를 실행,
story(플로어블 목록)를 잡아 HTML로 렌더한다. 본문/숫자는 원본 그대로 재사용된다.

사용: python rl2html.py <build_script.py> <out.html> "<기사 제목>" "<YYYY-MM-DD>" "<태그>"
"""
import sys, re, types, html as _html

# ---------- 가짜 reportlab ----------
class Color:
    def __init__(self, h): self.h = h
    def __eq__(self, o): return isinstance(o, Color) and o.h == self.h
    def __hash__(self): return hash(self.h)
class _colors:
    white = Color("#ffffff")
    @staticmethod
    def HexColor(h): return Color(h)
INK = "#1F2933"
class ParagraphStyle:
    def __init__(self, name, **kw):
        self.name = name; self.fontName = kw.get("fontName","KR")
        self.fontSize = kw.get("fontSize",9.2); self.textColor = kw.get("textColor", Color(INK))
        self.alignment = kw.get("alignment",0)
class Paragraph:
    def __init__(self, text, style=None): self.text = str(text); self.style = style
class Spacer:
    def __init__(self,*a,**k): pass
class PageBreak:
    def __init__(self,*a,**k): pass
class HRFlowable:
    def __init__(self,*a,**k): pass
class KeepTogether:
    def __init__(self, items): self.items = items if isinstance(items,(list,tuple)) else [items]
class TableStyle:
    def __init__(self, cmds=None): self.cmds = list(cmds or [])
class Table:
    def __init__(self, data, colWidths=None, repeatRows=0, **k):
        self.data = data; self.colWidths = colWidths; self.cmds = []
    def setStyle(self, ts): self.cmds = ts.cmds
class SimpleDocTemplate:
    captured = None
    def __init__(self, out, **k): self.out = out
    def build(self, story, **k):
        SimpleDocTemplate.captured = story
        try:
            open(self.out, "wb").write(b"\x00")  # 원본의 os.path.getsize(OUT) 호환용
        except Exception: pass
class pdfmetrics:
    @staticmethod
    def registerFont(*a, **k): pass
class TTFont:
    def __init__(self,*a,**k): pass
mm = 2.83465
A4 = (595.276, 841.890)
TA_LEFT, TA_CENTER, TA_RIGHT = 0, 1, 2

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k,v in attrs.items(): setattr(m,k,v)
    sys.modules[name] = m; return m
_mod("reportlab"); _mod("reportlab.lib")
_mod("reportlab.lib.pagesizes", A4=A4)
_mod("reportlab.lib.units", mm=mm)
_mod("reportlab.lib.colors", **{k:getattr(_colors,k) for k in dir(_colors) if not k.startswith("__")})
sys.modules["reportlab.lib.colors"].HexColor = _colors.HexColor
sys.modules["reportlab.lib.colors"].white = _colors.white
_mod("reportlab.lib.styles", ParagraphStyle=ParagraphStyle)
_mod("reportlab.lib.enums", TA_LEFT=TA_LEFT, TA_CENTER=TA_CENTER, TA_RIGHT=TA_RIGHT)
_mod("reportlab.pdfbase", pdfmetrics=pdfmetrics)
_mod("reportlab.pdfbase.ttfonts", TTFont=TTFont)
_mod("reportlab.pdfbase.pdfmetrics", registerFont=pdfmetrics.registerFont)
_mod("reportlab.platypus", SimpleDocTemplate=SimpleDocTemplate, Paragraph=Paragraph, Spacer=Spacer,
     Table=Table, TableStyle=TableStyle, KeepTogether=KeepTogether, HRFlowable=HRFlowable, PageBreak=PageBreak)

# ---------- 색/마크업 ----------
NAVY="#1B3C6E"; NAVY_HEAD="#264369"; GOLD="#C9A654"
def cstr(c): return c.h if isinstance(c,Color) else (c or INK)

def conv(text):
    """ReportLab 인라인 마크업 → HTML"""
    t = text
    t = re.sub(r'<font name="[^"]*">(.*?)</font>', r'\1', t)
    t = re.sub(r'<font size=7 color="([^"]+)">(.*?)</font>', r'<span style="font-size:.78em;color:\1">\2</span>', t)
    t = re.sub(r'<font color="([^"]+)">(.*?)</font>', r'<span style="color:\1">\2</span>', t)
    t = re.sub(r'<link href="([^"]+)"[^>]*>(.*?)</link>', r'<a href="\1" target="_blank" rel="noopener">\2</a>', t)
    return t

def cell_html(p, header=False):
    if not isinstance(p, Paragraph): return _html.escape(str(p))
    inner = conv(p.text)
    if header: return inner
    sty = p.style
    if sty is not None:
        col = cstr(sty.textColor)
        if sty.fontName == "KR-B": inner = f"<strong>{inner}</strong>"
        if col not in (INK, "#ffffff"): inner = f'<span style="color:{col}">{inner}</span>'
    return inner

# ---------- 스타일 명령 파서 ----------
def find_bg(cmds, want_whole=False, row0_only=False):
    for c in cmds:
        if c[0] == "BACKGROUND":
            (c0,r0),(c1,r1) = c[1], c[2]
            if want_whole and r0==0 and r1==-1 and c0==0 and c1==-1: return cstr(c[3])
            if row0_only and r0==0 and r1==0 and c0==0 and c1==-1: return cstr(c[3])
    return None
def find_linebefore(cmds):
    for c in cmds:
        if c[0] == "LINEBEFORE": return cstr(c[3])
    return None

def widths_pct(cw):
    if not cw: return None
    s = sum(cw)
    return [round(w/s*100, 3) for w in cw]

# ---------- Table 렌더 ----------
def render_table(t):
    data, cmds, cw = t.data, t.cmds, t.colWidths
    ncols = len(data[0]); nrows = len(data)
    whole = find_bg(cmds, want_whole=True)
    head = find_bg(cmds, row0_only=True)
    lb = find_linebefore(cmds)

    # 마스트헤드(1col·3row·NAVY 전체)
    if ncols==1 and nrows==3 and whole and whole.lower()==NAVY.lower():
        a = conv(data[0][0].text); b = conv(data[1][0].text); c = conv(data[2][0].text)
        return (f'<header class="art-hero"><div class="ah-k">{a}</div>'
                f'<div class="ah-t">{b}</div><div class="ah-s">{c}</div></header>')

    # 섹션 헤드(1col·2row·LINEBEFORE·배경 없음)
    if ncols==1 and nrows==2 and lb and not whole:
        lbl = conv(data[0][0].text); title = conv(data[1][0].text)
        return f'<div class="sec"><div class="eyebrow">{lbl}</div><h2>{title}</h2></div>'

    # 밴드/콜아웃(1col·2row·전체 배경·LINEBEFORE)
    if ncols==1 and nrows==2 and whole and lb:
        title = conv(data[0][0].text); body = conv(data[1][0].text)
        return (f'<div class="callout" style="background:{whole};border-left-color:{lb}">'
                f'<div class="callout-h" style="color:{lb}">{title}</div>'
                f'<div class="callout-b">{body}</div></div>')

    # 시나리오 바(1col 또는 2col·1row·전체 컬러 배경, navy_head 아님)
    if nrows==1 and whole and whole.lower()!=NAVY_HEAD.lower():
        if ncols==2:
            l = conv(data[0][0].text); r = conv(data[0][1].text)
            return (f'<div class="scenbar" style="background:{whole}">'
                    f'<span class="sb-l">{l}</span><span class="sb-r">{r}</span></div>')
        else:
            return f'<div class="scenbar" style="background:{whole}">{conv(data[0][0].text)}</div>'

    # 데이터 그리드(첫 행 NAVY_HEAD 배경)
    if head:
        cols = widths_pct(cw)
        colg = ""
        if cols: colg = "<colgroup>" + "".join(f'<col style="width:{p}%">' for p in cols) + "</colgroup>"
        thead = "<tr>" + "".join(f"<th>{cell_html(c,header=True)}</th>" for c in data[0]) + "</tr>"
        body = ""
        for r in data[1:]:
            body += "<tr>" + "".join(f"<td>{cell_html(c)}</td>" for c in r) + "</tr>"
        return f'<table class="grid">{colg}<thead>{thead}</thead><tbody>{body}</tbody></table>'

    # 2컬럼 키-값(요약카드 본문 / 용어풀이 / two_col). 전체 배경 있으면 입힘
    if ncols==2:
        cls = "kv" + (" kv-soft" if whole else "")
        style = f' style="background:{whole}"' if whole else ""
        rows = ""
        for r in data:
            rows += (f'<tr><th>{cell_html(r[0])}</th><td>{cell_html(r[1])}</td></tr>')
        return f'<table class="{cls}"{style}>{rows}</table>'

    # 단일행 컬러 없는 카드 헤더 등 — 일반 표로
    rows = ""
    for r in data:
        rows += "<tr>" + "".join(f"<td>{cell_html(c)}</td>" for c in r) + "</tr>"
    return f'<table class="grid plain">{rows}</table>'

# ---------- Paragraph(최상위) 렌더 ----------
PMAP = {
 "intro":('p','lead-para'), "body":('p',''), "small":('p','small'),
 "seclbl":('p','eyebrow-solo'), "blk":('h3','subhead'), "sec":('h2','subhead'),
 "disc":('p','disc'), "gh":('p','gloss-h'), "kk":('p','kakao'),
 "sc":('p','src-cat'), "link":('p','src-link'),
}
def render_par(p):
    name = p.style.name if p.style else "body"
    tag, cls = PMAP.get(name, ('p',''))
    inner = conv(p.text)
    align = ' style="text-align:center"' if (p.style and p.style.alignment==1) else ''
    c = f' class="{cls}"' if cls else ''
    return f'<{tag}{c}{align}>{inner}</{tag}>'

def render(fl):
    if isinstance(fl, Paragraph): return render_par(fl)
    if isinstance(fl, Table): return render_table(fl)
    if isinstance(fl, KeepTogether): return "".join(render(x) for x in fl.items)
    if isinstance(fl, HRFlowable): return '<hr class="rule">'
    return ""  # Spacer, PageBreak

# ---------- 실행 ----------
def main():
    script, out, title, date, tag = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], (sys.argv[5] if len(sys.argv)>5 else "리포트")
    src = open(script, encoding="utf-8").read()
    g = {"__name__":"__main__"}
    exec(compile(src, script, "exec"), g)
    story = SimpleDocTemplate.captured or []
    body = "".join(render(f) for f in story)

    KAKAO = "https://open.kakao.com/o/giw7dfAb"
    doc = f'''<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)} · INVEST STORY</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=Noto+Serif+KR:wght@600;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
 :root{{--navy:#1B3C6E;--gold:#C9A654;--gold-d:#a98731;--ink:#1F2933;--mute:#6B7785;--line:#E2E6EC;
  --serif:'Noto Serif KR',serif;--latin:'Playfair Display',serif;
  --sans:'Pretendard','Pretendard Variable',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:#F7F8FA;color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.72;-webkit-font-smoothing:antialiased}}
 a{{color:#1B5588}}
 .topbar{{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}}
 .topbar-in{{max-width:820px;margin:0 auto;padding:12px 22px;display:flex;align-items:center;justify-content:space-between}}
 .home{{font-family:var(--latin);font-weight:800;color:var(--navy);letter-spacing:.1em;text-decoration:none;font-size:16px}}
 .home:before{{content:"← ";color:var(--gold-d);font-family:var(--sans)}}
 .topbar .tag{{font-size:11.5px;font-weight:700;color:#fff;background:var(--navy);padding:3px 10px;border-radius:2px}}
 main{{max-width:820px;margin:0 auto;padding:6px 22px 60px}}
 .art-hero{{background:var(--navy);color:#fff;border-radius:4px;padding:26px 26px 28px;margin:18px 0 22px}}
 .art-hero .ah-k{{color:var(--gold);font-weight:800;font-size:12px;letter-spacing:.12em;margin-bottom:8px}}
 .art-hero .ah-t{{font-family:var(--serif);font-weight:900;font-size:clamp(22px,4.2vw,30px);line-height:1.32}}
 .art-hero .ah-s{{color:#cdd9ea;font-size:13px;margin-top:10px;line-height:1.6}}
 h2.subhead,h3.subhead{{font-family:var(--serif);color:var(--navy);font-weight:700;font-size:17px;margin:26px 0 10px;line-height:1.4}}
 .sec{{margin:34px 0 14px;border-top:2px solid var(--navy);padding-top:14px}}
 .sec .eyebrow{{color:var(--gold-d);font-weight:800;font-size:11.5px;letter-spacing:.1em;margin-bottom:4px}}
 .sec h2{{font-family:var(--serif);color:var(--navy);font-weight:900;font-size:clamp(19px,3.6vw,24px);margin:0;line-height:1.35}}
 .eyebrow-solo{{color:var(--gold-d);font-weight:800;font-size:12px;letter-spacing:.08em;margin:28px 0 6px}}
 p{{margin:0 0 13px}}
 .lead-para{{font-size:17px;line-height:1.85}}
 .small{{font-size:12.5px;color:var(--mute);line-height:1.6}}
 .disc{{font-size:11.5px;color:var(--mute);line-height:1.65}}
 table.grid{{width:100%;border-collapse:collapse;margin:6px 0 18px;font-size:13.5px;border:1px solid var(--line);table-layout:fixed}}
 table.grid th{{background:var(--navy);color:#fff;font-weight:700;text-align:left;padding:9px 10px;font-size:12.5px;vertical-align:top}}
 table.grid td{{padding:9px 10px;border-top:1px solid var(--line);vertical-align:top;word-break:keep-all}}
 table.grid tbody tr:nth-child(even){{background:#F4F6F8}}
 table.kv{{width:100%;border-collapse:collapse;margin:4px 0 16px;font-size:13.5px;border:1px solid var(--line)}}
 table.kv th{{width:34%;text-align:left;background:#F2F4F6;color:var(--ink);font-weight:700;padding:8px 10px;border-top:1px solid var(--line);border-right:1px solid var(--line);vertical-align:top;word-break:keep-all}}
 table.kv td{{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}}
 table.kv.kv-soft th{{background:transparent}}
 .scenbar{{color:#fff;border-radius:3px;padding:11px 16px;margin:16px 0 12px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:center}}
 .scenbar .sb-l{{font-weight:800;font-size:15px}}
 .scenbar .sb-r{{font-weight:700;font-size:14px;text-align:right;opacity:.95}}
 .callout{{border-left:3px solid;border-radius:3px;padding:13px 16px;margin:14px 0}}
 .callout-h{{font-weight:800;font-size:14px;margin-bottom:5px}}
 .callout-b{{font-size:14px;line-height:1.72}}
 .gloss-h{{font-family:var(--serif);font-weight:700;color:var(--navy);font-size:15px;margin:24px 0 6px}}
 .src-cat{{font-weight:800;color:var(--navy);font-size:13.5px;margin:14px 0 4px}}
 .src-link{{font-size:13px;margin:0 0 4px}}
 .kakao{{margin:22px 0}}
 hr.rule{{border:none;border-top:1px solid var(--line);margin:24px 0}}
 .artfoot{{max-width:820px;margin:0 auto;padding:22px;border-top:2px solid var(--navy);text-align:center}}
 .artfoot a.kk{{display:inline-block;background:#FEE500;color:#191600;font-weight:700;font-size:13.5px;padding:9px 16px;border-radius:3px;text-decoration:none;margin:6px 0 12px}}
 .artfoot .back{{display:inline-block;color:var(--navy);font-weight:700;text-decoration:none;font-size:13.5px}}
 .artfoot .dom{{color:var(--gold-d);font-weight:700;font-size:12px;letter-spacing:.06em;margin-top:10px}}
 @media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
 a:focus-visible{{outline:3px solid var(--gold);outline-offset:2px}}
</style></head><body>
<div class="topbar"><div class="topbar-in"><a class="home" href="/">INVEST STORY</a><span class="tag">{_html.escape(tag)} · {date}</span></div></div>
<main>
{body}
</main>
<footer class="artfoot">
 <a class="kk" href="{KAKAO}" target="_blank" rel="noopener">투자이야기 오픈채팅 바로가기</a><br>
 <a class="back" href="/">← 다른 리포트 보러가기</a>
 <div class="dom">investstory.co.kr</div>
</footer>
</body></html>'''
    open(out, "w", encoding="utf-8").write(doc)
    print(f"[rl2html] {out} 생성 · 플로어블 {len(story)}개")

if __name__ == "__main__":
    main()
