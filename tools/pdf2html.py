# -*- coding: utf-8 -*-
"""pdf2html.py — Invest Story 뉴스레터 PDF → 사이트 CSS HTML 기사.
   pdfplumber로 표/텍스트 추출 후 문단 병합 + 구조 인식하여 렌더."""
import sys, re, html as H, statistics, pdfplumber

KAKAO = "https://open.kakao.com/o/giw7dfAb"
ART_CSS = r'''
 :root{--navy:#1B3C6E;--gold:#C9A654;--gold-d:#a98731;--ink:#1F2933;--mute:#6B7785;--line:#E2E6EC;
  --serif:'Noto Serif KR',serif;--latin:'Playfair Display',serif;
  --sans:'Pretendard','Pretendard Variable',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;}
 *{box-sizing:border-box}
 body{margin:0;background:#F7F8FA;color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.72;-webkit-font-smoothing:antialiased}
 a{color:#1B5588}
 .topbar{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
 .topbar-in{max-width:820px;margin:0 auto;padding:12px 22px;display:flex;align-items:center;justify-content:space-between}
 .home{font-family:var(--latin);font-weight:800;color:var(--navy);letter-spacing:.1em;text-decoration:none;font-size:16px}
 .home:before{content:"← ";color:var(--gold-d);font-family:var(--sans)}
 .topbar .tag{font-size:11.5px;font-weight:700;color:#fff;background:var(--navy);padding:3px 10px;border-radius:2px}
 main{max-width:820px;margin:0 auto;padding:6px 22px 60px}
 .art-hero{background:var(--navy);color:#fff;border-radius:4px;padding:26px 26px 28px;margin:18px 0 22px}
 .art-hero .ah-k{color:var(--gold);font-weight:800;font-size:12px;letter-spacing:.12em;margin-bottom:8px}
 .art-hero .ah-t{font-family:var(--serif);font-weight:900;font-size:clamp(22px,4.2vw,30px);line-height:1.32}
 .art-hero .ah-s{color:#cdd9ea;font-size:13px;margin-top:10px;line-height:1.6}
 h2.subhead,h3.subhead{font-family:var(--serif);color:var(--navy);font-weight:700;font-size:17px;margin:26px 0 10px;line-height:1.4}
 .sec{margin:34px 0 14px;border-top:2px solid var(--navy);padding-top:14px}
 .sec .eyebrow{color:var(--gold-d);font-weight:800;font-size:11.5px;letter-spacing:.1em;margin-bottom:4px}
 .sec h2{font-family:var(--serif);color:var(--navy);font-weight:900;font-size:clamp(19px,3.6vw,24px);margin:0;line-height:1.35}
 .eyebrow-solo{color:var(--gold-d);font-weight:800;font-size:12px;letter-spacing:.08em;margin:28px 0 6px}
 p{margin:0 0 13px}
 .lead-para{font-size:17px;line-height:1.85}
 .small{font-size:12.5px;color:var(--mute);line-height:1.6}
 .disc{font-size:11.5px;color:var(--mute);line-height:1.65}
 table.grid{width:100%;border-collapse:collapse;margin:6px 0 18px;font-size:13.5px;border:1px solid var(--line);table-layout:fixed}
 table.grid th{background:var(--navy);color:#fff;font-weight:700;text-align:left;padding:9px 10px;font-size:12.5px;vertical-align:top}
 table.grid td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:top;word-break:keep-all}
 table.grid tbody tr:nth-child(even){background:#F4F6F8}
 table.kv{width:100%;border-collapse:collapse;margin:4px 0 16px;font-size:13.5px;border:1px solid var(--line)}
 table.kv th{width:34%;text-align:left;background:#F2F4F6;color:var(--ink);font-weight:700;padding:8px 10px;border-top:1px solid var(--line);border-right:1px solid var(--line);vertical-align:top;word-break:keep-all}
 table.kv td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
 table.kv.kv-soft th{background:transparent}
 .scenbar{color:#fff;border-radius:3px;padding:11px 16px;margin:16px 0 12px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:center}
 .scenbar .sb-l{font-weight:800;font-size:15px}
 .scenbar .sb-r{font-weight:700;font-size:14px;text-align:right;opacity:.95}
 .callout{border-left:3px solid;border-radius:3px;padding:13px 16px;margin:14px 0}
 .callout-h{font-weight:800;font-size:14px;margin-bottom:5px}
 .callout-b{font-size:14px;line-height:1.72}
 .gloss-h{font-family:var(--serif);font-weight:700;color:var(--navy);font-size:15px;margin:24px 0 6px}
 .src-cat{font-weight:800;color:var(--navy);font-size:13.5px;margin:14px 0 4px}
 .src-link{font-size:13px;margin:0 0 4px}
 .kakao{margin:22px 0}
 hr.rule{border:none;border-top:1px solid var(--line);margin:24px 0}
 .artfoot{max-width:820px;margin:0 auto;padding:22px;border-top:2px solid var(--navy);text-align:center}
 .artfoot a.kk{display:inline-block;background:#FEE500;color:#191600;font-weight:700;font-size:13.5px;padding:9px 16px;border-radius:3px;text-decoration:none;margin:6px 0 12px}
 .artfoot .back{display:inline-block;color:var(--navy);font-weight:700;text-decoration:none;font-size:13.5px}
 .artfoot .dom{color:var(--gold-d);font-weight:700;font-size:12px;letter-spacing:.06em;margin-top:10px}
 @media (prefers-reduced-motion:reduce){*{transition:none!important}}
 a:focus-visible{outline:3px solid var(--gold);outline-offset:2px}
'''
def esc(s): return H.escape((s or ""), quote=True)

RUNHDR   = re.compile(r'^JOSH PARK INVEST\s*·')
ALERTHDR = re.compile(r'^⚠')
DATELINE = re.compile(r'20\d\d년\s*\d+월\s*\d+일.*·')
SECLINE  = re.compile(r'^(★\s*)?SECTION\b', re.I)
STOCKHDR = re.compile(r'^\d{6}\s+\S')
SUBHEAD  = re.compile(r'^(\d+-\d+\.)')
EYEBROW  = re.compile(r'^(한눈에 핵심|주요 지표 대시보드|초보자 용어 풀이|초보자 용어)$|^(한눈에 핵심|주요 지표 대시보드|초보자 용어 풀이)\b')
SRC_HEAD = re.compile(r'^(참고\s*자료|Sources?\s*&?\s*References?)\b', re.I)
DISC_HEAD= re.compile(r'^면책\s*조항')
PUB_LINE = re.compile(r'^발행처\s*:|^발행\s*빈도|^\s*발행처: Josh')
KAKAO_LN = re.compile(r'오픈방|오픈채팅|잡담, 자랑')
MINILBL  = re.compile(r'^(지금 시장에서는|핵심 근거|오늘 흐름·예상|발생 조건|대응 포인트)\b')

def is_structural(t):
    return bool(SECLINE.match(t) or STOCKHDR.match(t) or SUBHEAD.match(t)
        or EYEBROW.match(t) or SRC_HEAD.match(t) or DISC_HEAD.match(t)
        or t.startswith('•') or PUB_LINE.match(t) or KAKAO_LN.search(t)
        or DISC_HEAD.match(t))

def render_table(rows):
    rows = [[(c or '').replace('\n',' ').strip() for c in r] for r in rows]
    rows = [r for r in rows if any(c for c in r)]
    if not rows: return ''
    ncol = max(len(r) for r in rows)
    rows = [r + ['']*(ncol-len(r)) for r in rows]
    if ncol == 2:
        body = ''.join(f'<tr><th>{esc(r[0])}</th><td>{esc(r[1])}</td></tr>' for r in rows)
        return f'<table class="kv">{body}</table>'
    head = ''.join(f'<th>{esc(c)}</th>' for c in rows[0])
    body = ''.join('<tr>'+''.join(f'<td>{esc(c)}</td>' for c in r)+'</tr>' for r in rows[1:])
    return f'<table class="grid"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

def page_items(page):
    """ordered list of ('table',rows) or ('line',top,bottom,text)"""
    tbls = page.find_tables()
    boxes = [t.bbox for t in tbls]
    items = [('table', t.bbox[1], t.bbox[3], t.extract()) for t in tbls]
    try: tlines = page.extract_text_lines(layout=False)
    except Exception: tlines = []
    for ln in tlines:
        cy = (ln['top']+ln['bottom'])/2; cx=(ln['x0']+ln['x1'])/2
        if any(top-1<=cy<=bot+1 and x0-1<=cx<=x1+1 for (x0,top,x1,bot) in boxes):
            continue
        items.append(('line', ln['top'], ln['bottom'], ln['text']))
    items.sort(key=lambda b: b[1])
    return items

def convert(pdf_path, title, tag, date):
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page_items(p) for p in pdf.pages]

    flat = []   # (kind, top, bottom, payload, page_idx)
    for pi, items in enumerate(pages):
        for it in items:
            if it[0]=='table': flat.append(('table',it[1],it[2],it[3],pi))
            else: flat.append(('line',it[1],it[2],it[3],pi))

    # masthead — may be a navy TABLE (special) or plain LINES (daily)
    DATE_IN=re.compile(r'20\d\d년\s*\d+월\s*\d+일')
    ah_s=''; start=0; mast_idx=None
    # (a) masthead rendered as a table
    for idx,(k,top,bot,pay,pi) in enumerate(flat):
        if k=='table':
            cells=[(c or '') for row in pay for c in row]
            if any('JOSH PARK INVEST' in c for c in cells):
                mast_idx=idx
                for c in cells:
                    for sub in str(c).split("\n"):
                        if DATE_IN.search(sub): ah_s=sub.strip(); break
                    if ah_s: break
                start=idx+1
                break
    # (b) masthead as standalone lines
    if mast_idx is None:
        for idx,(k,top,bot,pay,pi) in enumerate(flat):
            if k=='line' and pay.strip()=='JOSH PARK INVEST':
                mast_idx=idx; break
        if mast_idx is not None:
            seen=0
            for i in range(mast_idx+1,min(mast_idx+6,len(flat))):
                k,top,bot,pay,pi=flat[i]
                if k!='line': continue
                t=pay.strip()
                if not t or RUNHDR.match(t): continue
                seen+=1
                if DATE_IN.search(t): ah_s=t; start=i+1; break
                if seen>=3: break
    if not ah_s:  # last-resort fallback
        for i,(k,top,bot,pay,pi) in enumerate(flat):
            if k!='line': continue
            t=pay.strip()
            if RUNHDR.match(t) or ALERTHDR.match(t): continue
            if DATELINE.search(t) and '·' in t:
                ah_s=t; start=i+1; break
            if seen>=3: break
    if not ah_s:  # fallback
        for i,(k,top,bot,pay,pi) in enumerate(flat):
            if k!='line': continue
            t=pay.strip()
            if RUNHDR.match(t) or ALERTHDR.match(t): continue
            if DATELINE.search(t) and '·' in t:
                ah_s=t; start=i+1; break

    out=[]
    out.append(f'<header class="art-hero"><div class="ah-k">JOSH PARK INVEST · {esc(tag)}</div>'
               f'<div class="ah-t">{esc(title)}</div><div class="ah-s">{esc(ah_s)}</div></header>')

    # paragraph buffer
    buf=[]; intro_done=False; in_disc=False; skip_title=None
    prev_bottom=None; prev_pi=None; prev_h=12

    def flush():
        nonlocal buf,intro_done,in_disc
        if not buf: return
        txt=' '.join(buf).strip(); buf=[]
        if not txt: return
        if in_disc: out.append(f'<p class="disc">{esc(txt)}</p>'); return
        if not intro_done:
            out.append(f'<p class="lead-para">{esc(txt)}</p>'); intro_done=True; return
        out.append(f'<p>{esc(txt)}</p>')

    i=start
    while i < len(flat):
        k,top,bot,pay,pi = flat[i]
        if k=='table':
            flush(); out.append(render_table(pay)); prev_bottom=bot; prev_pi=pi; i+=1; continue
        t=pay.strip()
        if not t: i+=1; continue
        if RUNHDR.match(t) or ALERTHDR.match(t): i+=1; continue
        if PUB_LINE.match(t): i+=1; continue
        if KAKAO_LN.search(t): i+=1; continue
        if skip_title and t==skip_title: skip_title=None; prev_bottom=bot; prev_pi=pi; i+=1; continue

        h=max(bot-top,6)
        # SECTION divider
        if SECLINE.match(t):
            flush()
            lbl=t; title_txt=''
            j=i+1
            while j<len(flat):
                kk,tp,bt,p2,pj=flat[j]
                if kk=='line':
                    s2=p2.strip()
                    if s2 and not RUNHDR.match(s2): title_txt=s2; break
                else: break
                j+=1
            out.append(f'<div class="sec"><div class="eyebrow">{esc(lbl)}</div><h2>{esc(title_txt)}</h2></div>')
            skip_title=title_txt; prev_bottom=bot; prev_pi=pi; i+=1; continue
        if EYEBROW.match(t):
            flush(); out.append(f'<p class="eyebrow-solo">{esc(t)}</p>'); prev_bottom=bot; prev_pi=pi; i+=1; continue
        if SRC_HEAD.match(t):
            flush(); out.append(f'<p class="src-cat">{esc(t)}</p>'); prev_bottom=bot; prev_pi=pi; i+=1; continue
        if DISC_HEAD.match(t):
            flush(); in_disc=True
            out.append('<p class="src-cat">면책 조항</p>')
            rest=re.sub(r'^면책\s*조항\s*','',t).strip()
            if rest: buf.append(rest)
            prev_bottom=bot; prev_pi=pi; i+=1; continue
        if t.startswith('•'):
            flush(); out.append(f'<p class="src-link">{esc(t)}</p>'); prev_bottom=bot; prev_pi=pi; i+=1; continue
        if STOCKHDR.match(t):
            flush(); out.append(f'<h3 class="subhead">{esc(t)}</h3>'); prev_bottom=bot; prev_pi=pi; i+=1; continue
        if SUBHEAD.match(t):
            flush(); out.append(f'<h3 class="subhead">{esc(t)}</h3>'); prev_bottom=bot; prev_pi=pi; i+=1; continue
        if MINILBL.match(t):
            flush(); out.append(f'<p class="gloss-h">{esc(t)}</p>'); prev_bottom=bot; prev_pi=pi; i+=1; continue

        # plain text → paragraph buffer with gap-based splitting
        if prev_bottom is not None and pi==prev_pi:
            gap = top - prev_bottom
            if gap > 0.75*prev_h and buf:
                flush()
        elif pi!=prev_pi and buf:
            # page change: keep merging only if previous line had no terminal punctuation
            prevtxt = buf[-1] if buf else ''
            if re.search(r'[.。!?…”\)]\s*$', prevtxt) or re.search(r'[다요음함됨임\.]$', prevtxt):
                flush()
        buf.append(t)
        prev_bottom=bot; prev_pi=pi; prev_h=h
        i+=1
    flush()

    body="\n".join(out)
    doc=f'''<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · INVEST STORY</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=Noto+Serif+KR:wght@600;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>{ART_CSS}</style></head><body>
<div class="topbar"><div class="topbar-in"><a class="home" href="/">INVEST STORY</a><span class="tag">{esc(tag)} · {esc(date)}</span></div></div>
<main>
{body}
</main>
<footer class="artfoot">
 <a class="kk" href="{KAKAO}" target="_blank" rel="noopener">투자이야기 오픈채팅 바로가기</a><br>
 <a class="back" href="/">← 다른 리포트 보러가기</a>
 <div class="dom">investstory.co.kr</div>
</footer>
</body></html>'''
    return doc

if __name__=="__main__":
    pdf,out,title,tag,date=sys.argv[1:6]
    open(out,"w",encoding="utf-8").write(convert(pdf,title,tag,date))
    print(f"[pdf2html] {out}")
