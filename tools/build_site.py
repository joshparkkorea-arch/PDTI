# -*- coding: utf-8 -*-
"""
build_site.py — INVEST STORY 홈페이지 생성기
manifest.json(발간 호 목록)과 ticker.json(시세 스트립)을 읽어 index.html을 만든다.
publish.py가 자동 호출한다. 단독 실행도 가능: python tools/build_site.py
"""
import json, os, html, datetime, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
MANIFEST = os.path.join(ROOT, "manifest.json")
TICKER   = os.path.join(ROOT, "ticker.json")
OUT      = os.path.join(ROOT, "index.html")
PDTI_PATH = "pdti.html"             # 투자성향 테스트 앱 (루트에 파일로 유지 — 이미지 경로 보존)
KAKAO = "https://open.kakao.com/o/giw7dfAb"
DOMAIN = "investstory.co.kr"
WD = ["월","화","수","목","금","토","일"]

def kdate(s, longfmt=True):
    y,m,d = [int(x) for x in s.split("-")]
    dt = datetime.date(y,m,d)
    if longfmt:
        return f"{y}년 {m}월 {d}일 ({WD[dt.weekday()]})"
    return f"{m:02d}.{d:02d} ({WD[dt.weekday()]})"

def esc(s): return html.escape(str(s), quote=True)

def load(path, default):
    if not os.path.exists(path): return default
    with open(path, encoding="utf-8") as f: return json.load(f)

def main():
    man = load(MANIFEST, {"publication":"INVEST STORY","tagline":"","issues":[]})
    tk  = load(TICKER, None)
    issues = sorted(man.get("issues", []), key=lambda x:(x["date"], x.get("no",0)), reverse=True)
    pub = man.get("publication","INVEST STORY")
    tagline = man.get("tagline","")

    # ----- 시세 티커 -----
    ticker_html = ""
    if tk and tk.get("items"):
        cells = []
        for it in tk["items"]:
            arrow = "▲" if it.get("dir")=="up" else ("▼" if it.get("dir")=="down" else "·")
            cells.append(
              f'<span class="tk" data-n="{esc(it["name"])}"><span class="tk-n">{esc(it["name"])}</span>'
              f'<span class="tk-v">{esc(it["value"])}</span>'
              f'<span class="tk-c {esc(it.get("dir","flat"))}">{arrow}&nbsp;{esc(it["change"])}</span></span>')
        asof = esc(tk.get("asof",""))
        cells_html = "".join(cells)
        # 좌측 고정 라벨(asof/상태) + 우측 마퀴(동일한 종목 그룹 2벌을 이어붙여 무한 스크롤)
        ticker_html = (f'<div class="ticker">'
                       f'<span class="tk-as">{asof}</span>'
                       f'<div class="tk-mq"><div class="tk-track">'
                       f'<div class="tk-grp" id="tkmain">{cells_html}</div>'
                       f'<div class="tk-grp tk-clone" aria-hidden="true">{cells_html}</div>'
                       f'</div></div></div>')

    # 최신 발간일(이 날짜에 올라온 특집·특보는 '당일 업로드'로 보고 New 배지 + 미리보기 제외)
    newest_date = issues[0]["date"] if issues else None

    # 미리보기 카드(데일리·특집 공용)
    def card(a):
        return f'''
        <article class="lead">
          <a class="lead-link" href="{esc(a["file"])}">
            <div class="lead-meta">
              <span class="chip">{esc(a.get("tag","리포트"))}</span>
              <span class="lead-date">{kdate(a["date"])} · 제 {a.get("no","")}호</span>
            </div>
            <h2 class="lead-title">{esc(a["title"])}</h2>
            <p class="lead-sum">{esc(a.get("summary",""))}</p>
          </a>
        </article>'''

    # ----- 상단 미리보기: 가장 최근 '뉴스 리포트'(데일리 또는 특보) -----
    dailies = [a for a in issues if a.get("tag") == "데일리"]
    news_pool = [a for a in issues if a.get("tag") in ("데일리", "특보")]
    lead_top = news_pool[0] if news_pool else None
    lead_html = card(lead_top) if lead_top else '<p class="empty">아직 발간된 리포트가 없습니다.</p>'

    # ----- 그 아래 미리보기: '가장 최근 특집호' (당일 업로드분 포함) -----
    spec_pool = [a for a in issues if a.get("tag") == "특집호"]
    lead_special = spec_pool[0] if spec_pool else None
    special_html = card(lead_special) if lead_special else '<p class="empty">최신 특집·기획 리포트가 곧 이곳에 소개됩니다.</p>'

    # ----- 지난 호 (데일리만 — 상단 미리보기로 쓴 호는 제외) -----
    rows = []
    for a in dailies:
        if lead_top and a.get("no") == lead_top.get("no") and a.get("date") == lead_top.get("date"):
            continue
        rows.append(f'''
        <a class="row" href="{esc(a["file"])}">
          <span class="row-date">{kdate(a["date"], False)}</span>
          <span class="row-no">제 {a.get("no","")}호</span>
          <span class="row-tag">{esc(a.get("tag",""))}</span>
          <span class="row-body">
            <span class="row-title">{esc(a["title"])}</span>
            <span class="row-sum">{esc(a.get("summary",""))}</span>
          </span>
          <span class="row-pdf">읽기 →</span>
        </a>''')
    archive_html = "".join(rows) if rows else '<p class="empty">지난 데일리 리포트가 쌓이면 이곳에 아카이브됩니다.</p>'

    # ----- 특집·기획 사이드바 (데일리 제외 전부) · 당일 업로드분은 우측 상단 New 배지 -----
    sp_items = []
    for a in issues:
        if a.get("tag") == "데일리":
            continue
        tagcls = " founder" if a.get("tag") == "창간호" else ""
        new_badge = '<span class="side-new">NEW</span>' if a["date"] == newest_date else ''
        sp_items.append(f'''<a class="side-item" href="{esc(a["file"])}">
            {new_badge}<span class="side-tag{tagcls}">{esc(a.get("tag",""))}</span>
            <span class="side-title">{esc(a["title"])}</span>
            <span class="side-date">{kdate(a["date"], False)}</span>
          </a>''')
    side_html = ""
    if sp_items:
        side_html = ('<aside class="side"><div class="side-card">'
                     '<div class="side-head"><div class="side-k">SPECIAL</div>'
                     '<div class="side-t">특집 · 기획</div></div>'
                     '<div class="side-list">' + "".join(sp_items) + '</div></div></aside>')
    home_open = '<div class="home-grid">' if side_html else '<div class="home-solo">'

    today = kdate(issues[0]["date"]) if issues else kdate(datetime.date.today().isoformat())
    cur_no = issues[0].get("no","") if issues else ""

    page = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INVEST STORY · 투자이야기 데일리 리포트</title>
<meta name="description" content="{esc(tagline)} — 매일 발간되는 투자 리포트 아카이브.">
<meta property="og:title" content="INVEST STORY">
<meta property="og:description" content="{esc(tagline)}">
<meta property="og:type" content="website">
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
  body{{margin:0;background:var(--paper-2);color:var(--ink);font-family:var(--sans);
    font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}}
  a{{color:inherit;text-decoration:none}}
  .wrap{{max-width:980px;margin:0 auto;padding:0 22px}}

  /* ---- 마스트헤드 ---- */
  .masthead{{background:var(--paper);border-bottom:1px solid var(--line)}}
  .mast-in{{padding:30px 22px 20px;text-align:center}}
  .rule{{height:2px;background:var(--gold);max-width:980px;margin:0 auto}}
  .rule.thin{{height:1px;background:var(--line-2)}}
  .wordmark{{font-family:var(--latin);font-weight:900;color:var(--navy);
    font-size:clamp(40px,8vw,76px);letter-spacing:.14em;line-height:1;margin:16px 0 8px;text-indent:.14em}}
  .submast{{font-family:var(--serif);font-weight:600;color:var(--ink);font-size:clamp(13px,2.4vw,16px);letter-spacing:.02em}}
  .issueline{{margin-top:7px;color:var(--mute);font-size:12.5px;letter-spacing:.08em;text-transform:none}}
  .issueline b{{color:var(--gold-d);font-weight:700}}

  /* ---- 티커 ---- */
  .ticker{{background:var(--navy);color:#eaf0f7;display:flex;align-items:center;overflow:hidden}}
  .ticker::-webkit-scrollbar{{display:none}}
  .tk-as{{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9fb4d0;font-size:11.5px;letter-spacing:.04em;padding:9px 16px 9px 22px;border-right:1px solid #34507e}}
  .tk-mq{{flex:1 1 auto;overflow:hidden}}
  .tk-track{{display:flex;width:max-content;animation:tkscroll 40s linear infinite;will-change:transform}}
  .tk-grp{{display:flex;gap:22px;align-items:center;white-space:nowrap;font-size:13px;padding-left:22px}}
  @keyframes tkscroll{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
  .ticker:hover .tk-track{{animation-play-state:paused}}
  @media (prefers-reduced-motion:reduce){{.tk-track{{animation:none}}}}
  @media (max-width:640px){{.tk-as{{max-width:44vw}}}}
  .tk{{display:inline-flex;gap:7px;align-items:baseline}}
  .tk-n{{color:#c7d6ea;font-weight:600;letter-spacing:.02em}}
  .tk-v{{font-weight:700;color:#fff;font-variant-numeric:tabular-nums}}
  .tk-c{{font-weight:700;font-variant-numeric:tabular-nums}}
  .tk-c.up{{color:#ff8a7a}} .tk-c.down{{color:#7fb6ef}} .tk-c.flat{{color:#c7d6ea}}
  .tk{{border-radius:3px}}
  @keyframes tkflash{{0%{{background:rgba(201,166,84,0)}}22%{{background:rgba(201,166,84,.34)}}100%{{background:rgba(201,166,84,0)}}}}
  .tk-flash{{animation:tkflash .9s ease-out}}
  .tk-live{{color:#9fe3b0;font-weight:700}}
  .tk-delay{{color:#e0b96a;font-weight:700}}

  /* ---- 섹션 라벨 ---- */
  .eyebrow{{display:flex;align-items:center;gap:12px;margin:34px 0 16px}}
  .eyebrow h3{{font-family:var(--serif);font-weight:700;color:var(--navy);font-size:15px;letter-spacing:.06em;margin:0;white-space:nowrap}}
  .eyebrow:before{{content:"";width:18px;height:2px;background:var(--gold);flex:0 0 auto}}
  .eyebrow:after{{content:"";height:1px;background:var(--line);flex:1}}

  /* ---- 리드 ---- */
  .lead{{background:var(--paper);border:1px solid var(--line);border-top:3px solid var(--navy);
    border-radius:3px;box-shadow:0 1px 0 rgba(20,41,74,.04)}}
  .lead-link{{display:block;padding:26px 28px 28px;transition:background .15s}}
  .lead-link:hover{{background:#fcfcfd}}
  .lead-link:hover .lead-title{{text-decoration:underline;text-decoration-color:var(--gold);text-underline-offset:4px;text-decoration-thickness:2px}}
  .lead-meta{{display:flex;align-items:center;gap:12px;margin-bottom:14px}}
  .chip{{background:var(--navy);color:#fff;font-size:11.5px;font-weight:700;letter-spacing:.05em;
    padding:3px 10px;border-radius:2px}}
  .lead-date{{color:var(--mute);font-size:12.5px;letter-spacing:.04em;font-variant-numeric:tabular-nums}}
  .lead-title{{font-family:var(--serif);font-weight:900;color:var(--ink);
    font-size:clamp(23px,4.4vw,34px);line-height:1.28;letter-spacing:-.01em;margin:0 0 12px}}
  .lead-sum{{color:#3c4855;font-size:15.5px;line-height:1.72;margin:0 0 20px;max-width:60ch}}
  .btn-row{{display:flex;flex-wrap:wrap;gap:10px}}
  .btn{{display:inline-block;font-weight:700;font-size:14px;letter-spacing:.01em;padding:11px 18px;border-radius:3px;transition:transform .12s,box-shadow .12s}}
  .btn-primary{{background:var(--gold);color:#241a00}}
  .btn-ghost{{background:transparent;color:var(--navy);box-shadow:inset 0 0 0 1.5px var(--line-2)}}
  .lead-link:hover .btn-primary{{box-shadow:0 4px 14px rgba(201,166,84,.4)}}

  /* ---- 지난 호 ---- */
  .archive{{display:flex;flex-direction:column;border-top:1px solid var(--line)}}
  .row{{display:grid;grid-template-columns:74px 56px 64px 1fr auto;gap:14px;align-items:center;
    padding:16px 6px;border-bottom:1px solid var(--line);transition:background .12s}}
  .row:hover{{background:#fcfcfd}}
  .row-date{{color:var(--navy);font-weight:700;font-size:13px;font-variant-numeric:tabular-nums}}
  .row-no{{color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}}
  .row-tag{{font-size:11px;font-weight:700;color:var(--gold-d);border:1px solid var(--gold);
    border-radius:2px;padding:2px 7px;text-align:center;white-space:nowrap}}
  .row-title{{display:block;font-family:var(--serif);font-weight:700;color:var(--ink);font-size:16px;line-height:1.4}}
  .row-sum{{display:block;color:var(--mute);font-size:12.5px;line-height:1.55;margin-top:3px;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical}}
  .row-pdf{{color:var(--down);font-weight:700;font-size:12.5px;white-space:nowrap}}

  /* ---- 배너 CTA (투자성향 테스트) ---- */
  .banner{{margin:40px 0 10px;border-radius:4px;overflow:hidden;
    background:linear-gradient(100deg,var(--navy) 0%,var(--navy-2) 100%);position:relative}}
  .banner a{{display:flex;align-items:center;justify-content:space-between;gap:18px;
    padding:24px 26px;flex-wrap:wrap}}
  .banner:before{{content:"";position:absolute;inset:0;background:
    repeating-linear-gradient(135deg,rgba(201,166,84,.0) 0 18px,rgba(201,166,84,.06) 18px 19px)}}
  .banner-txt{{position:relative}}
  .banner-k{{color:var(--gold);font-size:12px;font-weight:700;letter-spacing:.14em;margin-bottom:5px}}
  .banner-t{{color:#fff;font-family:var(--serif);font-weight:700;font-size:clamp(18px,3.4vw,23px);line-height:1.3}}
  .banner-cta{{position:relative;background:var(--gold);color:#241a00;font-weight:800;font-size:14.5px;
    padding:12px 20px;border-radius:3px;white-space:nowrap}}
  .banner a:hover .banner-cta{{box-shadow:0 4px 16px rgba(201,166,84,.45)}}

  /* ---- 홈 2단 레이아웃 + 특집 사이드바 ---- */
  .home-grid{{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:36px;align-items:start;margin-top:6px}}
  .home-solo{{margin-top:6px}}
  .home-main{{min-width:0}}
  .side{{position:sticky;top:18px}}
  .side-card{{background:var(--paper);border:1px solid var(--line);border-top:3px solid var(--gold);border-radius:3px;overflow:hidden}}
  .side-head{{background:var(--navy);padding:13px 16px}}
  .side-k{{color:var(--gold);font-size:10px;font-weight:800;letter-spacing:.14em}}
  .side-t{{font-family:var(--serif);color:#fff;font-weight:700;font-size:17px;margin-top:2px}}
  .side-list{{padding:2px 16px 10px}}
  .side-item{{display:block;padding:13px 0;border-bottom:1px solid var(--line);position:relative}}
  .side-item:last-child{{border-bottom:none}}
  .side-new{{position:absolute;top:12px;right:0;background:#E5392E;color:#fff;font-size:9px;font-weight:800;
    letter-spacing:.06em;line-height:1;padding:3px 5px;border-radius:2px;box-shadow:0 1px 3px rgba(224,57,46,.35)}}
  .side-tag{{display:inline-block;font-size:10px;font-weight:700;color:var(--gold-d);border:1px solid var(--gold);border-radius:2px;padding:1px 7px}}
  .side-tag.founder{{color:#fff;background:var(--navy);border-color:var(--navy)}}
  .side-title{{display:block;font-family:var(--serif);font-weight:700;color:var(--ink);font-size:14px;line-height:1.42;margin:7px 0 3px}}
  .side-item:hover .side-title{{text-decoration:underline;text-decoration-color:var(--gold);text-underline-offset:3px;text-decoration-thickness:2px}}
  .side-date{{color:var(--mute);font-size:11px;font-variant-numeric:tabular-nums}}
  @media (max-width:860px){{
    .home-grid{{grid-template-columns:1fr;gap:24px}}
    .side{{position:static}}
  }}

  /* ---- 푸터 ---- */
  footer{{margin-top:46px;border-top:2px solid var(--navy);background:var(--paper)}}
  .foot-in{{padding:26px 22px 40px;text-align:center}}
  .foot-mark{{font-family:var(--latin);font-weight:800;color:var(--navy);letter-spacing:.12em;font-size:18px;text-indent:.12em}}
  .foot-pub{{color:var(--mute);font-size:12.5px;margin:8px 0 14px}}
  .foot-kakao{{display:inline-block;background:#FEE500;color:#191600;font-weight:700;font-size:13.5px;
    padding:10px 18px;border-radius:3px;margin-bottom:16px}}
  .foot-kakao:hover{{filter:brightness(.97)}}
  .disclaimer{{color:var(--mute);font-size:11px;line-height:1.7;max-width:70ch;margin:0 auto}}
  .foot-dom{{color:var(--gold-d);font-weight:700;font-size:12px;letter-spacing:.06em;margin-top:12px}}

  @media (max-width:640px){{
    .row{{grid-template-columns:64px 1fr auto;row-gap:4px}}
    .row-no,.row-tag{{display:none}}
    .banner a{{justify-content:center;text-align:center}}
  }}
  @media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
  a:focus-visible,.lead-link:focus-visible{{outline:3px solid var(--gold);outline-offset:2px;border-radius:2px}}
</style>
</head>
<body>
  <header class="masthead">
    <div class="rule"></div>
    <div class="mast-in">
      <div class="wordmark">INVEST&nbsp;STORY</div>
      <div class="submast">{esc(tagline)}</div>
      <div class="issueline">{today} &nbsp;·&nbsp; <b>제 {cur_no}호</b> &nbsp;·&nbsp; 발행 Josh Park Invest</div>
    </div>
    <div class="rule thin"></div>
  </header>

  {ticker_html}

  <main class="wrap">
    {home_open}
      <div class="home-main">
        <div class="eyebrow"><h3>오늘의 리포트</h3></div>
        {lead_html}

        <div class="eyebrow"><h3>특집 · 기획</h3></div>
        {special_html}

        <div class="eyebrow"><h3>지난 호</h3></div>
        <div class="archive">
          {archive_html}
        </div>

        <div class="banner">
          <a href="{PDTI_PATH}">
            <span class="banner-txt">
              <div class="banner-k">INVEST STORY · INTERACTIVE</div>
              <div class="banner-t">나의 투자 성향은? — 16가지 투자 유형 테스트</div>
            </span>
            <span class="banner-cta">테스트 시작하기 →</span>
          </a>
        </div>
      </div>
      {side_html}
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
    # 배너 실시간 갱신 스크립트(f-string 밖 일반 문자열 — 중괄호 충돌 방지)
    ticker_script = """
  <script>
  /* 시세 스트립 실시간 갱신 (3단 폴백)
     1) Twelve Data: ticker_config.json의 twelvedata_key가 있으면 우선 사용(KOSPI=KS11 등).
     2) 야후(무키): CORS 프록시 경유 best-effort.
     3) ticker.json 시드값: 위가 모두 실패해도 절대 빈 화면 없음.
     - 탭이 보일 때 + 장중 위주로만 호출(무료 한도 절약).
     - 값이 바뀌면 칸이 잠깐 반짝이고, 좌측에 LIVE/지연 + 시각 표시. */
  (function(){
    var CFG = {
      'KOSPI':     {sym:'^KS11',    td:'KS11',    dec:2, mode:'pct', mkt:'kr'},
      'KOSDAQ':    {sym:'^KQ11',    td:'KQ11',    dec:2, mode:'pct', mkt:'kr'},
      'USD/KRW':   {sym:'KRW=X',    td:'USD/KRW', dec:1, mode:'abs', mkt:'fx'},
      'WTI':       {sym:'CL=F',     td:'WTI/USD', dec:2, mode:'pct', prefix:'$', mkt:'us'},
      'S&P 500':   {sym:'^GSPC',    td:'GSPC',    dec:2, mode:'pct', mkt:'us'},
      '\\uB098\\uC2A4\\uB2E5':       {sym:'^IXIC', td:'IXIC', dec:2, mode:'pct', mkt:'us'},
      '\\uB2EC\\uB7EC\\uC778\\uB371\\uC2A4': {sym:'DX-Y.NYB', td:'DXY', dec:2, mode:'pct', mkt:'us'}
    };
    var NBSP = String.fromCharCode(160);
    var PROXY = [
      function(u){ return u; },                                                              /* 직접 */
      function(u){ return 'https://api.codetabs.com/v1/proxy/?quest=' + encodeURIComponent(u); },
      function(u){ return 'https://api.allorigins.win/raw?url=' + encodeURIComponent(u); },
      function(u){ return 'https://thingproxy.freeboard.io/fetch/' + u; }
    ];
    var baseAsof = '';
    var liveOK = {};
    var tdKey = '', tdDead = false;
    var tdByName = {}, tdNames = {};
    Object.keys(CFG).forEach(function(name){ tdByName[name]=CFG[name].td; tdNames[CFG[name].td]=name; });

    function fmt(n, dec){ return Number(n).toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
    function arrowOf(d){ return d==='up' ? '\\u25B2' : (d==='down' ? '\\u25BC' : '\\u00B7'); }

    function syncClone(){
      var m=document.querySelector('#tkmain'), c=document.querySelector('.tk-clone');
      if(m&&c) c.innerHTML=m.innerHTML;        /* 보이지 않는 두번째 그룹을 항상 동일하게 유지 → 끊김 없는 무한 스크롤 */
    }
    function renderSeed(tk){
      var el=document.querySelector('#tkmain'); if(!el||!tk||!tk.items) return;
      baseAsof = tk.asof || '';
      var asEl=document.querySelector('.tk-as'); if(asEl) asEl.textContent=baseAsof;
      el.innerHTML='';
      tk.items.forEach(function(it){
        var dir=it.dir||'flat';
        var w=document.createElement('span'); w.className='tk'; w.setAttribute('data-n', it.name);
        var n=document.createElement('span'); n.className='tk-n'; n.textContent=it.name;
        var v=document.createElement('span'); v.className='tk-v'; v.textContent=it.value;
        var c=document.createElement('span'); c.className='tk-c '+dir; c.textContent=arrowOf(dir)+NBSP+(it.change||'');
        w.appendChild(n); w.appendChild(v); w.appendChild(c); el.appendChild(w);
      });
      syncClone();
    }
    function findCell(name){
      var all=document.querySelectorAll('#tkmain .tk'); var hit=null;
      all.forEach(function(x){
        if(hit) return;
        if(x.getAttribute('data-n')===name){ hit=x; return; }
        var n=x.querySelector('.tk-n');               /* data-n 없는 시드 칸도 종목명으로 매칭 */
        if(n && n.textContent.trim()===name) hit=x;
      });
      return hit;
    }
    function patch(name, value, change, dir){
      var w=findCell(name); if(!w) return;
      var v=w.querySelector('.tk-v'), c=w.querySelector('.tk-c');
      var changed = (v && v.textContent!==value);
      if(v) v.textContent=value;
      if(c){ c.className='tk-c '+dir; c.textContent=arrowOf(dir)+NBSP+change; }
      if(changed){ w.classList.remove('tk-flash'); void w.offsetWidth; w.classList.add('tk-flash'); }
      syncClone();
    }
    function applyQuote(name, close, change, pct){
      var cf=CFG[name]; if(!cf) return;
      var dir = pct>0?'up':(pct<0?'down':'flat');
      var val=(cf.prefix||'')+fmt(close, cf.dec);
      var chg=(cf.mode==='abs') ? ((change>=0?'+':'')+fmt(change,1)) : ((pct>=0?'+':'')+Math.abs(pct).toFixed(2)+'%');
      patch(name, val, chg, dir); liveOK[name]=Date.now();
    }

    function getJson(url){            /* 프록시 체인(야후/ticker.json용) */
      var i=0;
      function go(){
        if(i>=PROXY.length) return Promise.reject();
        return fetch(PROXY[i++](url), {cache:'no-store'})
          .then(function(r){ if(!r.ok) throw 0; return r.text(); })
          .then(function(t){ return JSON.parse(t); }).catch(go);
      }
      return go();
    }

    /* ---- 1) Twelve Data (키 있을 때 우선, CORS 지원 → 직접 호출) ---- */
    function tdFetch(names){
      if(!tdKey || tdDead || !names.length) return Promise.resolve([]);
      var syms=names.map(function(n){return tdByName[n];}).join(',');
      var url='https://api.twelvedata.com/quote?symbol='+encodeURIComponent(syms)+'&apikey='+encodeURIComponent(tdKey);
      return fetch(url,{cache:'no-store'}).then(function(r){ if(!r.ok) throw 0; return r.json(); }).then(function(j){
        if(!j) return [];
        if(j.status==='error' || j.code===401 || j.code===429){ tdDead=true; return []; }
        var filled=[];
        function handle(sym, q){
          if(!q || q.status==='error') return;
          var name=tdNames[sym] || (q.symbol && tdNames[q.symbol]); if(!name) return;
          var close=parseFloat(q.close), pct=parseFloat(q.percent_change), chg=parseFloat(q.change);
          if(isNaN(close)) return;
          if(isNaN(pct)) pct=0; if(isNaN(chg)) chg=0;
          applyQuote(name, close, chg, pct); filled.push(name);
        }
        if(names.length===1){ handle(tdByName[names[0]], j); }
        else { Object.keys(j).forEach(function(sym){ handle(sym, j[sym]); }); }
        return filled;
      }).catch(function(){ return []; });
    }

    /* ---- 2) 야후(무키) 폴백 ---- */
    function yahooOne(name){
      var cf=CFG[name]; if(!cf) return Promise.resolve(false);
      var hosts=['query1.finance.yahoo.com','query2.finance.yahoo.com'];
      function tryHost(hi){
        if(hi>=hosts.length) return Promise.resolve(false);
        var url='https://'+hosts[hi]+'/v8/finance/chart/'+encodeURIComponent(cf.sym)+'?range=1d&interval=5m';
        return getJson(url).then(function(j){
          var m=j&&j.chart&&j.chart.result&&j.chart.result[0]&&j.chart.result[0].meta; if(!m) return tryHost(hi+1);
          var price=(typeof m.regularMarketPrice==='number')?m.regularMarketPrice:null;
          var prev=(typeof m.chartPreviousClose==='number')?m.chartPreviousClose:((typeof m.previousClose==='number')?m.previousClose:null);
          if(price===null||prev===null) return tryHost(hi+1);
          var diff=price-prev; applyQuote(name, price, diff, (prev?diff/prev*100:0)); return true;
        }).catch(function(){ return tryHost(hi+1); });
      }
      return tryHost(0);
    }

    function setStatus(mode){   /* 'live' | 'auto' | 'delay' */
      var as=document.querySelector('.tk-as'); if(!as) return;
      var t=new Date(), p=function(x){return ('0'+x).slice(-2);};
      var hhmmss=p(t.getHours())+':'+p(t.getMinutes())+':'+p(t.getSeconds());
      var cls=(mode==='delay')?'tk-delay':'tk-live';
      var word=(mode==='live')?'LIVE':((mode==='auto')?'\\uC790\\uB3D9':'\\uC9C0\\uC5F0'); /* 자동 / 지연 */
      as.innerHTML=(baseAsof?baseAsof+'  \\u00B7  ':'')+'<span class="'+cls+'">\\u27F3 '+word+' '+hhmmss+'</span>';
    }

    /* ---- 시장 개장(KST) 판단 ---- */
    function marketState(){
      var t=new Date(), u=t.getTime()+t.getTimezoneOffset()*60000, k=new Date(u+9*3600000);
      var day=k.getDay(), hm=k.getHours()*60+k.getMinutes(), weekday=(day>=1&&day<=5);
      var isKR=weekday && hm>=540 && hm<945;             /* 09:00~15:45 KST */
      var isUS=weekday && (hm>=1350 || hm<360);          /* 22:30~익일 06:00 KST (서머타임 포함) */
      var names=[];
      Object.keys(CFG).forEach(function(name){
        var m=CFG[name].mkt;
        if((m==='kr'&&isKR)||(m==='us'&&isUS)||(m==='fx'&&(isKR||isUS))) names.push(name);
      });
      return {names:names, anyOpen:(isKR||isUS)};
    }

    function liveRound(names){
      return tdFetch(names).then(function(filled){
        var need=names.filter(function(n){ return filled.indexOf(n)<0; });
        var jobs=need.map(function(name, idx){
          return new Promise(function(res){ setTimeout(function(){ yahooOne(name).then(res).catch(function(){res(false);}); }, idx*250); });
        });
        return Promise.all(jobs).then(function(rs){ return (filled.length>0) || rs.some(function(x){return x===true;}); });
      });
    }

    function cycle(){
      getJson('ticker.json?t='+Date.now()).then(function(tk){
        if(tk&&tk.items){
          if(!document.querySelector('#tkmain .tk')){ renderSeed(tk); }
          else { baseAsof=tk.asof||baseAsof;
            tk.items.forEach(function(it){
              if(liveOK[it.name] && (Date.now()-liveOK[it.name]<300000)) return;
              patch(it.name, it.value, it.change||'', it.dir||'flat');
            });
          }
        }
      }).catch(function(){}).then(function(){
        if(document.hidden) return;                 /* 라이브 API는 보일 때만(한도 절약) */
        var s=marketState();
        var names = s.names.length ? s.names : Object.keys(CFG);
        liveRound(names).then(function(any){
          var serverFresh = /\\uC790\\uB3D9/.test(baseAsof);   /* ticker.json asof에 '자동' 포함 = 서버 갱신 중 */
          setStatus(any ? 'live' : (serverFresh ? 'auto' : 'delay'));
        });
      });
    }

    var timer=null;
    function schedule(){
      if(timer) clearTimeout(timer);
      var ms = marketState().anyOpen ? 180000 : 1200000;   /* 장중 3분 · 폐장 20분 */
      timer=setTimeout(function(){ cycle(); schedule(); }, ms);
    }
    function start(){ cycle(); schedule(); }

    fetch('ticker_config.json?t='+Date.now(),{cache:'no-store'})
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(cfg){ if(cfg && typeof cfg.twelvedata_key==='string') tdKey=cfg.twelvedata_key.trim(); })
      .catch(function(){})
      .then(start);

    document.addEventListener('visibilitychange', function(){ if(!document.hidden){ cycle(); } });
  })();
  </script>
"""
    if "</body>" in page:
        page = page.replace("</body>", ticker_script + "</body>", 1)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"[build_site] index.html 생성 완료 · 발간 호 {len(issues)}건")

if __name__ == "__main__":
    main()
