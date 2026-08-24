# -*- coding: utf-8 -*-
"""
build_site.py — INVEST STORY 홈페이지 생성기 (v11 · 1면 재설계)
manifest.json(발간 호 목록)과 ticker.json(시세 스트립)을 읽어 index.html을 만든다.
- 핵심 지표: 정적 카드 3줄 9종(코스피·코스닥·달러 / WTI·S&P·나스닥 / 달러인덱스·KRX 금·국제 금).
  2026-08-24: '지표 더보기' 접기 UI 폐기 — 전 지표를 첫 화면에 한 번에 노출.
  (마퀴 폐기. 단, 장중 실시간 갱신 엔진 Twelve Data+야후는 그대로 유지 → 정적 카드를 patch.)
- 카테고리 탭(전체·데일리·특집·기획·특보)으로 카드 그리드 필터(같은 페이지 JS).
- 오늘의 리포트 = 대표 카드(최신 호). 색: 데일리 네이비 / 특집·기획 골드 / 특보 레드.
단독 실행: python tools/build_site.py
"""
import json, os, html, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifest.json")
TICKER   = os.path.join(ROOT, "ticker.json")
OUT      = os.path.join(ROOT, "index.html")
PDTI_PATH = "pdti.html"
KAKAO = "https://open.kakao.com/o/giw7dfAb"
DOMAIN = "investstory.co.kr"
WD = ["월","화","수","목","금","토","일"]
KST = datetime.timezone(datetime.timedelta(hours=9))

# 지표 표시 순서·한글 라벨 (ticker.json name → 라벨).
# 2026-08-24: 접힘(더보기) 없이 9종 전부를 한 번에 노출. 3열 그리드에 정확히 3줄로 떨어진다.
STAT_ORDER = [
    ("KOSPI", "코스피"), ("KOSDAQ", "코스닥"), ("USD/KRW", "달러·원"),
    ("WTI", "WTI 유가"), ("S&P 500", "S&P 500"), ("나스닥", "나스닥"),
    ("달러인덱스", "달러인덱스"), ("KRX 금", "KRX 금 (원/g)"), ("국제 금", "국제 금 ($/oz)"),
]

# 카테고리 버킷·색 (탭 필터 + 태그/악센트 색)
def cat_of(tag):
    if tag == "데일리": return "daily"
    if tag == "특보":   return "flash"
    return "special"    # 특집호·창간호·기획 등
CAT_COLOR = {"daily": ("#1B3C6E", "#ffffff"), "flash": ("#C0392B", "#ffffff"), "special": ("#C9A654", "#3d2f08")}

def kdate(s, longfmt=True):
    y,m,d = [int(x) for x in s.split("-")]
    dt = datetime.date(y,m,d)
    return f"{y}년 {m}월 {d}일 ({WD[dt.weekday()]})" if longfmt else f"{m:02d}.{d:02d} ({WD[dt.weekday()]})"

def kdatetime(a, longfmt=True):
    base = kdate(a["date"], longfmt); t = a.get("time")
    return f"{base} {t}" if t else base

def esc(s): return html.escape(str(s), quote=True)

def load(path, default):
    if not os.path.exists(path): return default
    with open(path, encoding="utf-8") as f: return json.load(f)

def main():
    man = load(MANIFEST, {"publication":"INVEST STORY","tagline":"","issues":[]})
    tk  = load(TICKER, None)
    issues = sorted(man.get("issues", []), key=lambda x:(x["date"], x.get("no",0)), reverse=True)
    tagline = man.get("tagline","")

    # ---------- 핵심 지표 카드 ----------
    by_name = {}
    if tk and tk.get("items"):
        for it in tk["items"]: by_name[it.get("name")] = it
    stat_cells = []
    for name, label in STAT_ORDER:
        it = by_name.get(name, {})
        val = esc(it.get("value","—")); chg = esc(it.get("change","")); dr = esc(it.get("dir","flat"))
        arrow = "▲" if dr=="up" else ("▼" if dr=="down" else "·")
        stat_cells.append(
            f'<div class="stat" data-n="{esc(name)}">'
            f'<span class="stat-n">{esc(label)}</span>'
            f'<span class="stat-v">{val}</span>'
            f'<span class="stat-c {dr}">{arrow}&nbsp;{chg}</span></div>')
    stat_cards_html = "".join(stat_cells)
    asof = esc(tk.get("asof","")) if tk else ""

    # ---------- 카드(공용) ----------
    present = {cat_of(a.get("tag","")) for a in issues}
    today_iso = datetime.datetime.now(KST).date().isoformat()

    def feat_html(a, cat, label):
        c = cat_of(a.get("tag",""))
        bg, fg = CAT_COLOR[c]
        tag = esc(a.get("tag","리포트"))
        meta = f'{kdatetime(a, False)} · 제 {a.get("no","")}호'
        hid = "" if cat == "all" else " hidden"
        return f'''<a class="feature cat-{c}" href="{esc(a["file"])}" style="--cat:{bg}" data-feat="{cat}" data-label="{esc(label)}" data-file="{esc(a["file"])}"{hid}>
  <span class="feat-tag" style="background:{bg};color:{fg}">{tag}</span>
  <h2 class="feat-title">{esc(a["title"])}</h2>
  <p class="feat-sum">{esc(a.get("summary",""))}</p>
  <span class="feat-meta">{esc(meta)}</span>
  <span class="feat-cta">전문 읽기 →</span>
</a>'''

    def card_html(a):
        c = cat_of(a.get("tag",""))
        bg, fg = CAT_COLOR[c]
        tag = esc(a.get("tag","리포트"))
        meta = f'{kdatetime(a, True)} · 제 {a.get("no","")}호'
        return f'''<a class="ncard" href="{esc(a["file"])}" data-cat="{c}" data-file="{esc(a["file"])}">
  <span class="ncard-tag" style="background:{bg};color:{fg}">{tag}</span>
  <span class="ncard-title">{esc(a["title"])}</span>
  <span class="ncard-sum">{esc(a.get("summary",""))}</span>
  <span class="ncard-meta">{esc(meta)}</span>
</a>'''

    def newest_in(cat):
        for a in issues:
            if cat == "all" or cat_of(a.get("tag","")) == cat:
                return a
        return None
    def label_for(a):
        return "오늘의 리포트" if a and a.get("date") == today_iso else "최신 리포트"

    # 카테고리별 대표 카드(전체 + 있는 카테고리). 탭 클릭 시 JS가 해당 카드를 보여줌.
    feat_specs = [("all", issues[0] if issues else None)]
    for _c in ("daily","special","flash"):
        if _c in present:
            feat_specs.append((_c, newest_in(_c)))
    features_html = "".join(feat_html(a, cat, label_for(a)) for cat, a in feat_specs if a) \
        if issues else '<p class="empty">아직 발간된 리포트가 없습니다.</p>'
    init_label = label_for(issues[0]) if issues else "오늘의 리포트"

    # 카드 그리드 = 전체 호(JS가 탭별 필터 + 대표 카드 중복 제거)
    cards_html = "".join(card_html(a) for a in issues) if issues \
        else '<p class="empty" data-empty="all">리포트가 쌓이면 이곳에 정리됩니다.</p>'

    tab_defs = [("all","전체")] + [(c,l) for c,l in (("daily","데일리"),("special","특집·기획"),("flash","특보")) if c in present]
    tabs_html = "".join(
        f'<button class="tab{" on" if c=="all" else ""}" data-tab="{c}">{esc(l)}</button>' for c,l in tab_defs)

    today = kdate(issues[0]["date"]) if issues else kdate(datetime.date.today().isoformat())
    cur_no = issues[0].get("no","") if issues else ""

    page = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INVEST STORY · 투자이야기 데일리 리포트</title>
<meta name="description" content="{esc(tagline)} — 매일 발간되는 투자 리포트.">
<meta property="og:title" content="INVEST STORY">
<meta property="og:description" content="{esc(tagline)}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://investstory.co.kr/">
<meta property="og:image" content="https://investstory.co.kr/assets/og-default.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://investstory.co.kr/assets/og-default.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800;900&family=Noto+Serif+KR:wght@600;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
  :root{{
    --navy:#1B3C6E; --navy-2:#13294a; --gold:#C9A654; --gold-d:#a98731;
    --ink:#1F2933; --mute:#6B7785; --line:#E2E6EC; --line-2:#cfd6df;
    --up:#C0392B; --down:#1B5E9B; --paper:#ffffff; --paper-2:#F7F8FA;
    --serif:'Noto Serif KR', serif; --latin:'Playfair Display', serif;
    --sans:'Pretendard','Pretendard Variable',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
  }}
  *{{box-sizing:border-box}}
  html{{-webkit-text-size-adjust:100%}}
  body{{margin:0;background:var(--paper-2);color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}}
  a{{color:inherit;text-decoration:none}}
  .wrap{{max-width:980px;margin:0 auto;padding:0 22px}}

  /* ---- 마스트헤드 ---- */
  .masthead{{background:var(--paper);border-bottom:1px solid var(--line)}}
  .rule{{height:2px;background:var(--gold);max-width:980px;margin:0 auto}}
  .mast-in{{padding:24px 22px 16px;text-align:center}}
  .wordmark{{display:inline-block;font-family:var(--latin);font-weight:900;color:var(--navy);
    font-size:clamp(34px,6.6vw,58px);letter-spacing:.14em;line-height:1;margin:6px 0 6px;text-indent:.14em;transition:opacity .15s}}
  .wordmark:hover{{opacity:.82}}
  .submast{{font-family:var(--serif);font-weight:600;color:var(--ink);font-size:clamp(12px,2.2vw,15px)}}
  .issueline{{margin-top:6px;color:var(--mute);font-size:12px;letter-spacing:.06em}}
  .issueline b{{color:var(--gold-d);font-weight:700}}

  /* ---- 카테고리 탭(스티키) ---- */
  .topnav{{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.94);backdrop-filter:saturate(1.2) blur(6px);
    border-bottom:1px solid var(--line)}}
  .topnav-in{{max-width:980px;margin:0 auto;padding:0 16px;display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}}
  .topnav-in::-webkit-scrollbar{{display:none}}
  .tab{{appearance:none;background:none;border:none;cursor:pointer;font-family:var(--sans);
    font-size:14px;font-weight:600;color:var(--mute);padding:13px 14px;white-space:nowrap;
    border-bottom:2.5px solid transparent;transition:color .12s,border-color .12s}}
  .tab:hover{{color:var(--ink)}}
  .tab.on{{color:var(--navy);border-bottom-color:var(--gold)}}

  main{{padding-top:6px}}
  .sec-k{{font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--gold-d);margin:26px 0 11px}}

  /* ---- 핵심 지표 카드 ---- */
  .tk-status{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:22px 0 9px;font-size:11.5px;color:var(--mute)}}
  .tk-live{{color:#1a8f4a;font-weight:700}} .tk-delay{{color:var(--gold-d);font-weight:700}}
  .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
  .stat{{background:var(--paper);border:1px solid var(--line);border-radius:7px;padding:12px 14px;display:flex;flex-direction:column;gap:2px}}
  .stat-n{{font-size:11.5px;color:var(--mute)}}
  .stat-v{{font-size:clamp(17px,3.6vw,21px);font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}
  .stat-c{{font-size:12.5px;font-weight:700;font-variant-numeric:tabular-nums}}
  .stat-c.up{{color:var(--up)}} .stat-c.down{{color:var(--down)}} .stat-c.flat{{color:var(--mute)}}
  @keyframes stflash{{0%{{background:rgba(201,166,84,0)}}25%{{background:rgba(201,166,84,.22)}}100%{{background:rgba(201,166,84,0)}}}}
  .stat.flash{{animation:stflash 1s ease-out}}

  /* ---- 오늘의 리포트(대표 카드) ---- */
  .feature{{display:block;background:var(--paper);border:1px solid var(--line);border-left:5px solid var(--cat,var(--navy));
    border-radius:8px;padding:22px 24px 24px;transition:box-shadow .15s}}
  .feature:hover{{box-shadow:0 6px 22px rgba(20,41,74,.08)}}
  .feature[hidden]{{display:none}}
  .feat-tag{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;padding:3px 10px;border-radius:3px}}
  .feat-title{{font-family:var(--serif);font-weight:900;color:var(--ink);font-size:clamp(22px,4.2vw,31px);line-height:1.3;letter-spacing:-.01em;margin:12px 0 11px}}
  .feat-sum{{color:#3c4855;font-size:15px;line-height:1.72;margin:0;max-width:62ch}}
  .feat-meta{{display:block;color:var(--mute);font-size:12.5px;margin-top:13px;font-variant-numeric:tabular-nums}}
  .feat-cta{{display:inline-block;margin-top:15px;background:var(--gold);color:#3d2f08;font-weight:800;font-size:14px;padding:11px 22px;border-radius:5px;transition:box-shadow .12s}}
  .feature:hover .feat-cta{{box-shadow:0 5px 16px rgba(201,166,84,.4)}}

  /* ---- 통일 카드 그리드 ---- */
  .cardgrid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
  .ncard{{display:flex;flex-direction:column;background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:15px 16px;transition:box-shadow .14s,transform .14s}}
  .ncard:hover{{box-shadow:0 5px 16px rgba(20,41,74,.07);transform:translateY(-1px)}}
  .ncard-tag{{align-self:flex-start;font-size:10.5px;font-weight:700;letter-spacing:.03em;padding:3px 9px;border-radius:3px}}
  .ncard-title{{font-family:var(--serif);font-weight:700;color:var(--ink);font-size:15.5px;line-height:1.4;margin:9px 0 5px}}
  .ncard-sum{{color:var(--mute);font-size:12.5px;line-height:1.55;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}}
  .ncard-meta{{color:#9aa3ae;font-size:11px;margin-top:auto;padding-top:10px;font-variant-numeric:tabular-nums}}
  .ncard[hidden]{{display:none}}
  .grid-empty{{color:var(--mute);font-size:13.5px;padding:26px 4px;text-align:center;border:1px dashed var(--line-2);border-radius:8px}}

  /* ---- 배너 CTA ---- */
  .banner{{margin:34px 0 8px;border-radius:8px;overflow:hidden;background:linear-gradient(100deg,var(--navy) 0%,var(--navy-2) 100%);position:relative}}
  .banner a{{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:22px 24px;flex-wrap:wrap}}
  .banner-k{{color:var(--gold);font-size:11.5px;font-weight:700;letter-spacing:.13em;margin-bottom:5px}}
  .banner-t{{color:#fff;font-family:var(--serif);font-weight:700;font-size:clamp(17px,3.2vw,22px);line-height:1.3}}
  .banner-cta{{background:var(--gold);color:#241a00;font-weight:800;font-size:14px;padding:11px 19px;border-radius:5px;white-space:nowrap}}

  /* ---- 푸터 ---- */
  footer{{margin-top:42px;border-top:2px solid var(--navy);background:var(--paper)}}
  .foot-in{{padding:26px 22px 40px;text-align:center}}
  .foot-mark{{font-family:var(--latin);font-weight:800;color:var(--navy);letter-spacing:.12em;font-size:17px;text-indent:.12em}}
  .foot-pub{{color:var(--mute);font-size:12px;margin:7px 0 13px}}
  .foot-kakao{{display:inline-block;background:#FEE500;color:#191600;font-weight:700;font-size:13px;padding:10px 18px;border-radius:4px;margin-bottom:15px}}
  .disclaimer{{color:var(--mute);font-size:11px;line-height:1.7;max-width:70ch;margin:0 auto}}
  .foot-dom{{color:var(--gold-d);font-weight:700;font-size:12px;letter-spacing:.06em;margin-top:11px}}

  @media (max-width:640px){{
    .stats{{grid-template-columns:1fr 1fr;gap:8px}}
    /* 2열에서 지표 수가 홀수면 마지막 한 장이 반쪽으로 남는다 → 한 줄 전체로 펴서 마감. */
    .stats .stat:last-child:nth-child(odd){{grid-column:1 / -1}}
    .cardgrid{{grid-template-columns:1fr}}
    .banner a{{justify-content:center;text-align:center}}
  }}
  @media (prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
  a:focus-visible,.tab:focus-visible{{outline:3px solid var(--gold);outline-offset:2px;border-radius:3px}}
</style>
</head>
<body>
  <header class="masthead">
    <div class="rule"></div>
    <div class="mast-in">
      <a class="wordmark" href="/">INVEST&nbsp;STORY</a>
      <div class="submast">{esc(tagline)}</div>
      <div class="issueline">{today} &nbsp;·&nbsp; <b>제 {cur_no}호</b> &nbsp;·&nbsp; 발행 Josh Park Invest</div>
    </div>
  </header>

  <nav class="topnav"><div class="topnav-in">{tabs_html}</div></nav>

  <main class="wrap">
    <div class="tk-status"><span id="tk-asof">{asof}</span><span id="tk-live"></span></div>
    <div class="stats">{stat_cards_html}</div>

    <div class="sec-k" id="feat-k">{init_label}</div>
    <div id="features">{features_html}</div>

    <div class="sec-k" id="grid-k">최신 글</div>
    <div class="cardgrid" id="cardgrid">{cards_html}</div>
    <div class="grid-empty" id="grid-empty" hidden>이 카테고리의 다른 글은 아직 없어요.</div>

    <div class="banner">
      <a href="{PDTI_PATH}">
        <span class="banner-txt">
          <div class="banner-k">INVEST STORY · INTERACTIVE</div>
          <div class="banner-t">나의 투자 성향은? — 16가지 투자 유형 테스트</div>
        </span>
        <span class="banner-cta">테스트 시작하기 →</span>
      </a>
    </div>
  </main>

  <footer>
    <div class="foot-in">
      <div class="foot-mark">INVEST&nbsp;STORY</div>
      <div class="foot-pub">{esc(tagline)} · 발행처 Josh Park Invest</div>
      <a class="foot-kakao" href="{KAKAO}" target="_blank" rel="noopener">투자이야기 오픈채팅 바로가기</a>
      <p class="disclaimer">본 사이트의 모든 리포트는 시장 분석 및 정보 제공 목적이며, 특정 종목의 매수·매도 권유가 아닙니다.
      모든 투자 결정과 그 결과의 책임은 투자자 본인에게 있습니다. 가격·지표는 각 리포트 발행 시점 기준이며 이후 달라질 수 있습니다.</p>
      <div class="foot-dom">{DOMAIN}</div>
    </div>
  </footer>
</body>
</html>'''

    page = page.replace("</body>", SCRIPT + "</body>", 1)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"[build_site] index.html 생성 완료 · 발간 호 {len(issues)}건")


# 탭 필터 + 시세 실시간 갱신(엔진 유지, 정적 카드를 patch) — f-string 밖 일반 문자열
# 2026-08-24: '지표 더보기' 토글 블록 제거(전 지표 상시 노출).
SCRIPT = r"""
  <script>
  /* ===== 1) 카테고리 탭 필터 ===== */
  (function(){
    var tabs=document.querySelectorAll('.tab');
    var cards=document.querySelectorAll('#cardgrid .ncard');
    var feats=document.querySelectorAll('#features .feature');
    var empty=document.getElementById('grid-empty');
    var gk=document.getElementById('grid-k');
    var fk=document.getElementById('feat-k');
    var LBL={all:'최신 글',daily:'데일리',special:'특집 · 기획',flash:'특보'};
    function apply(cat){
      var featFile=null;
      feats.forEach(function(f){
        var on=(f.getAttribute('data-feat')===cat);
        f.hidden=!on;
        if(on){ featFile=f.getAttribute('data-file'); if(fk) fk.textContent=f.getAttribute('data-label')||'최신 리포트'; }
      });
      var shown=0;
      cards.forEach(function(c){
        var ok=(cat==='all'||c.getAttribute('data-cat')===cat)&&c.getAttribute('data-file')!==featFile;
        if(ok){c.hidden=false;shown++;}else{c.hidden=true;}
      });
      if(empty) empty.hidden=(shown>0);
      if(gk){ gk.style.display=shown>0?'':'none'; gk.textContent=LBL[cat]||'최신 글'; }
    }
    apply('all');
    tabs.forEach(function(t){
      t.addEventListener('click',function(){
        tabs.forEach(function(x){x.classList.remove('on');});
        t.classList.add('on');
        apply(t.getAttribute('data-tab'));
      });
    });
  })();

  /* ===== 2) 시세 실시간 갱신 (Twelve Data → 야후 → ticker.json) · 정적 카드 patch ===== */
  (function(){
    var CFG = {
      'KOSPI':     {sym:'^KS11',    td:'KS11',    dec:2, mode:'pct', mkt:'kr'},
      'KOSDAQ':    {sym:'^KQ11',    td:'KQ11',    dec:2, mode:'pct', mkt:'kr'},
      'USD/KRW':   {sym:'KRW=X',    td:'USD/KRW', dec:1, mode:'abs', mkt:'fx'},
      'WTI':       {sym:'CL=F',     td:'WTI/USD', dec:2, mode:'pct', prefix:'$', mkt:'us'},
      'S&P 500':   {sym:'^GSPC',    td:'GSPC',    dec:2, mode:'pct', mkt:'us'},
      '\uB098\uC2A4\uB2E5':       {sym:'^IXIC', td:'IXIC', dec:2, mode:'pct', mkt:'us'},
      '\uB2EC\uB7EC\uC778\uB371\uC2A4': {sym:'DX-Y.NYB', td:'DXY', dec:2, mode:'pct', mkt:'us'},
      /* 국제 금(현물 XAU/USD) — 24시간 거래라 국내·미국 어느 장이 열려도 갱신(mkt:'fx'). */
      '\uAD6D\uC81C \uAE08': {sym:'XAUUSD=X', td:'XAU/USD', dec:2, mode:'pct', prefix:'$', mkt:'fx'}
      /* 'KRX 금'은 브라우저에서 직접 칠 수 있는 무료 소스가 없어 CFG에 넣지 않는다.
         → 서버(update_ticker.py)가 채운 ticker.json을 cycle()이 매번 patch해 갱신한다. */
    };
    var NBSP=String.fromCharCode(160);
    var PROXY=[function(u){return u;},
      function(u){return 'https://api.codetabs.com/v1/proxy/?quest='+encodeURIComponent(u);},
      function(u){return 'https://api.allorigins.win/raw?url='+encodeURIComponent(u);}];
    var baseAsof=(document.getElementById('tk-asof')||{}).textContent||'';
    var liveOK={}, tdKey='', tdDead=false, tdByName={}, tdNames={};
    Object.keys(CFG).forEach(function(n){ tdByName[n]=CFG[n].td; tdNames[CFG[n].td]=n; });

    function fmt(n,dec){ return Number(n).toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
    function arrowOf(d){ return d==='up'?'\u25B2':(d==='down'?'\u25BC':'\u00B7'); }
    function findCard(name){
      var all=document.querySelectorAll('.stat'); var hit=null;
      all.forEach(function(x){ if(!hit && x.getAttribute('data-n')===name) hit=x; });
      return hit;
    }
    function patch(name,value,change,dir){
      var w=findCard(name); if(!w) return;
      var v=w.querySelector('.stat-v'), c=w.querySelector('.stat-c');
      var changed=(v && v.textContent!==value);
      if(v) v.textContent=value;
      if(c){ c.className='stat-c '+dir; c.textContent=arrowOf(dir)+NBSP+change; }
      if(changed){ w.classList.remove('flash'); void w.offsetWidth; w.classList.add('flash'); }
    }
    function applyQuote(name,close,change,pct){
      var cf=CFG[name]; if(!cf) return;
      var dir=pct>0?'up':(pct<0?'down':'flat');
      var val=(cf.prefix||'')+fmt(close,cf.dec);
      var chg=(cf.mode==='abs')?((change>=0?'+':'')+fmt(change,1)):((pct>=0?'+':'')+Math.abs(pct).toFixed(2)+'%');
      patch(name,val,chg,dir); liveOK[name]=Date.now();
    }
    function getJson(url){
      var i=0;
      function go(){ if(i>=PROXY.length) return Promise.reject();
        return fetch(PROXY[i++](url),{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.text();})
          .then(function(t){return JSON.parse(t);}).catch(go); }
      return go();
    }
    function tdFetch(names){
      if(!tdKey||tdDead||!names.length) return Promise.resolve([]);
      var syms=names.map(function(n){return tdByName[n];}).join(',');
      var url='https://api.twelvedata.com/quote?symbol='+encodeURIComponent(syms)+'&apikey='+encodeURIComponent(tdKey);
      return fetch(url,{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json();}).then(function(j){
        if(!j) return [];
        if(j.status==='error'||j.code===401||j.code===429){ tdDead=true; return []; }
        var filled=[];
        function handle(sym,q){ if(!q||q.status==='error') return;
          var name=tdNames[sym]||(q.symbol&&tdNames[q.symbol]); if(!name) return;
          var close=parseFloat(q.close),pct=parseFloat(q.percent_change),chg=parseFloat(q.change);
          if(isNaN(close)) return; if(isNaN(pct))pct=0; if(isNaN(chg))chg=0;
          applyQuote(name,close,chg,pct); filled.push(name); }
        if(names.length===1){ handle(tdByName[names[0]],j); }
        else { Object.keys(j).forEach(function(sym){ handle(sym,j[sym]); }); }
        return filled;
      }).catch(function(){return [];});
    }
    function yahooOne(name){
      var cf=CFG[name]; if(!cf) return Promise.resolve(false);
      var hosts=['query1.finance.yahoo.com','query2.finance.yahoo.com'];
      function tryHost(hi){ if(hi>=hosts.length) return Promise.resolve(false);
        var url='https://'+hosts[hi]+'/v8/finance/chart/'+encodeURIComponent(cf.sym)+'?range=1d&interval=5m';
        return getJson(url).then(function(j){
          var m=j&&j.chart&&j.chart.result&&j.chart.result[0]&&j.chart.result[0].meta; if(!m) return tryHost(hi+1);
          var price=(typeof m.regularMarketPrice==='number')?m.regularMarketPrice:null;
          var prev=(typeof m.chartPreviousClose==='number')?m.chartPreviousClose:((typeof m.previousClose==='number')?m.previousClose:null);
          if(price===null||prev===null) return tryHost(hi+1);
          var diff=price-prev; applyQuote(name,price,diff,(prev?diff/prev*100:0)); return true;
        }).catch(function(){return tryHost(hi+1);});
      }
      return tryHost(0);
    }
    function setStatus(mode){
      var el=document.getElementById('tk-live'); if(!el) return;
      var t=new Date(),p=function(x){return ('0'+x).slice(-2);};
      var hhmm=p(t.getHours())+':'+p(t.getMinutes())+':'+p(t.getSeconds());
      var cls=(mode==='delay')?'tk-delay':'tk-live';
      var word=(mode==='live')?'LIVE':((mode==='auto')?'\uC790\uB3D9':'\uC9C0\uC5F0');
      el.className=cls; el.textContent='\u00B7 \u27F3 '+word+' '+hhmm;
    }
    function marketState(){
      var t=new Date(),u=t.getTime()+t.getTimezoneOffset()*60000,k=new Date(u+9*3600000);
      var day=k.getDay(),hm=k.getHours()*60+k.getMinutes(),weekday=(day>=1&&day<=5);
      var isKR=weekday&&hm>=540&&hm<945, isUS=weekday&&(hm>=1350||hm<360);
      var names=[]; Object.keys(CFG).forEach(function(n){ var m=CFG[n].mkt;
        if((m==='kr'&&isKR)||(m==='us'&&isUS)||(m==='fx'&&(isKR||isUS))) names.push(n); });
      return {names:names, anyOpen:(isKR||isUS)};
    }
    function liveRound(names){
      return tdFetch(names).then(function(filled){
        var need=names.filter(function(n){return filled.indexOf(n)<0;});
        var jobs=need.map(function(name,idx){ return new Promise(function(res){
          setTimeout(function(){ yahooOne(name).then(res).catch(function(){res(false);}); }, idx*250); }); });
        return Promise.all(jobs).then(function(rs){ return (filled.length>0)||rs.some(function(x){return x===true;}); });
      });
    }
    function cycle(){
      getJson('ticker.json?t='+Date.now()).then(function(tk){
        if(tk&&tk.items){ baseAsof=tk.asof||baseAsof;
          var a=document.getElementById('tk-asof'); if(a) a.textContent=baseAsof;
          tk.items.forEach(function(it){
            if(liveOK[it.name]&&(Date.now()-liveOK[it.name]<300000)) return;
            patch(it.name,it.value,it.change||'',it.dir||'flat');
          });
        }
      }).catch(function(){}).then(function(){
        if(document.hidden) return;
        var s=marketState(); var names=s.names.length?s.names:Object.keys(CFG);
        liveRound(names).then(function(any){
          var serverFresh=/\uC790\uB3D9/.test(baseAsof);
          setStatus(any?'live':(serverFresh?'auto':'delay'));
        });
      });
    }
    var timer=null;
    function schedule(){ if(timer) clearTimeout(timer);
      var ms=marketState().anyOpen?180000:1200000;
      timer=setTimeout(function(){ cycle(); schedule(); }, ms); }
    function start(){ cycle(); schedule(); }
    fetch('ticker_config.json?t='+Date.now(),{cache:'no-store'})
      .then(function(r){return r.ok?r.json():null;})
      .then(function(cfg){ if(cfg&&typeof cfg.twelvedata_key==='string') tdKey=cfg.twelvedata_key.trim(); })
      .catch(function(){}).then(start);
    document.addEventListener('visibilitychange',function(){ if(!document.hidden){ cycle(); } });
  })();
  </script>
"""

if __name__ == "__main__":
    main()
