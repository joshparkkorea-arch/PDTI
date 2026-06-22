# -*- coding: utf-8 -*-
"""daily_news.py — INVEST STORY 데일리 자동 발행기 (개장 브리핑 / 마감 시황).

실행:
  python tools/daily_news.py close          # 마감 시황(장 종료 직후)
  python tools/daily_news.py open           # 개장 브리핑(장 시작 직후)
  python tools/daily_news.py close --force   # 휴장일 판정 무시하고 강제 생성
  python tools/daily_news.py close --selftest# 파일 안 쓰고 데이터 소스만 점검(권장: 첫 도입 시)

동작 개요
  1) 거래일 판정: KIS 휴장일조회 API(1순위) → 실패 시 주말+하드코딩 백업.
     거래일이 아니면 아무것도 만들지 않고 종료(코드 0).
  2) 지수: ticker.json(앞 단계 update_ticker.py가 갱신)에서 7종을 읽음.
  3) (마감) 등락주 TOP·외국인/기관 수급: KIS로 best-effort 수신. 실패한 섹션은
     기사에서 자동 생략(절대 크래시 안 함). 어떤 게 됐는지 로그로 남김.
  4) 기사 HTML 생성 → newsletters/ 에 저장.
  5) manifest.json 에 '데일리'로 등록(같은 날짜·모드면 갱신).
  6) build_site.py 재실행 → index.html 재생성.
  커밋/푸시는 워크플로(daily-news.yml)가 담당.
"""
import json, os, sys, time, html, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_DIR = os.path.join(ROOT, "newsletters")
MANIFEST = os.path.join(ROOT, "manifest.json")
TICKER   = os.path.join(ROOT, "ticker.json")
KST = timezone(timedelta(hours=9))
KIS_BASE = "https://openapi.koreainvestment.com:9443"
UA = "INVEST-STORY-daily/1.0"

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# KIS가 닿지 않을 때만 쓰는 백업 휴장일(2026, KRX 추정). KIS API가 1순위이므로 보조용.
# 확실치 않은 날은 KIS가 최종 판정하니, 의심되면 KIS 결과를 신뢰.
HOLIDAYS_FALLBACK = {
    "2026-01-01",  # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설날 연휴
    "2026-03-02",  # 삼일절 대체(3/1 일)
    "2026-05-01",  # 근로자의 날(증시 휴장)
    "2026-05-05",  # 어린이날
    "2026-05-25",  # 부처님오신날 대체(5/24 일)
    "2026-06-03",  # 지방선거(추정)
    "2026-08-17",  # 광복절 대체(8/15 토)
    "2026-09-24", "2026-09-25", "2026-09-28",  # 추석 연휴(+대체)
    "2026-10-05",  # 개천절 대체(10/3 토)
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
    "2026-12-31",  # 연말 휴장
}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


# ----------------------------- KIS -----------------------------
def kis_token(app_key, app_secret):
    body = json.dumps({"grant_type": "client_credentials",
                       "appkey": app_key, "appsecret": app_secret}).encode()
    req = urllib.request.Request(KIS_BASE + "/oauth2/tokenP", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def kis_get(path, tr_id, params, token, app_key, app_secret):
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    req = urllib.request.Request(KIS_BASE + path + ("?" + qs if qs else ""), headers={
        "Content-Type": "application/json", "User-Agent": UA,
        "authorization": "Bearer " + token,
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": tr_id, "custtype": "P",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


import urllib.parse  # (kis_get에서 사용)


def is_open_day_kis(date_kst, token, app_key, app_secret):
    """KIS 국내휴장일조회로 개장일 여부 판정. (개장이면 True, 휴장이면 False, 불명이면 None)"""
    try:
        ymd = date_kst.strftime("%Y%m%d")
        data = kis_get("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                       {"BASS_DT": ymd, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                       token, app_key, app_secret)
        if data.get("rt_cd") != "0":
            return None
        for o in data.get("output", []):
            if o.get("bass_dt") == ymd:
                return (o.get("opnd_yn") == "Y")   # 개장일여부
        return None
    except Exception as e:
        sys.stderr.write(f"[holiday] KIS 휴장일조회 실패: {e}\n")
        return None


def trading_day(date_kst, token, app_key, app_secret):
    """(거래일여부, 판정근거)"""
    if date_kst.weekday() >= 5:
        return False, "주말"
    if token:
        kis = is_open_day_kis(date_kst, token, app_key, app_secret)
        if kis is True:
            return True, "KIS 개장일"
        if kis is False:
            return False, "KIS 휴장일"
    # KIS 불명/토큰없음 → 백업
    if date_kst.strftime("%Y-%m-%d") in HOLIDAYS_FALLBACK:
        return False, "백업 휴장일표"
    return True, "백업(주말·휴장표 제외)"


def fetch_fluctuation(token, app_key, app_secret, sort_code, n=5):
    """등락률 순위. sort_code: '0'=상승률 상위, '1'=하락률 상위. 실패 시 None."""
    try:
        params = {
            "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": "0000", "fid_rank_sort_cls_code": sort_code,
            "fid_input_cnt_1": "0", "fid_prc_cls_code": "0",
            "fid_input_price_1": "", "fid_input_price_2": "",
            "fid_vol_cnt": "", "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0", "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "", "fid_rsfl_rate2": "",
        }
        data = kis_get("/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000",
                       params, token, app_key, app_secret)
        if data.get("rt_cd") != "0":
            return None
        out = []
        for o in data.get("output", [])[:n]:
            out.append({
                "name": o.get("hts_kor_isnm", "—"),
                "price": o.get("stck_prpr", "—"),
                "ctrt": o.get("prdy_ctrt", "—"),   # 등락률(%)
                "sign": o.get("prdy_vrss_sign", "3"),
            })
        return out or None
    except Exception as e:
        sys.stderr.write(f"[rank] 등락률({sort_code}) 실패: {e}\n")
        return None


def fetch_investors(token, app_key, app_secret):
    """시장(코스피/코스닥) 외국인·기관 순매수 — best-effort.
    KIS 계정/엔드포인트에 따라 응답이 다를 수 있어, 실패 시 None 반환하고 섹션 생략."""
    try:
        # 업종별 투자자 순매수: 시장구분 U, 코스피=0001 / 코스닥=1001
        out = {}
        for nm, iscd in (("코스피", "0001"), ("코스닥", "1001")):
            data = kis_get("/uapi/domestic-stock/v1/quotations/inquire-investor",
                           "FHPTJ04030000",
                           {"fid_cond_mrkt_div_code": "U", "fid_input_iscd": iscd},
                           token, app_key, app_secret)
            if data.get("rt_cd") != "0":
                continue
            rows = data.get("output", [])
            row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
            if not row:
                continue
            out[nm] = {
                "frgn": row.get("frgn_ntby_qty") or row.get("frgn_ntby_tr_pbmn") or "—",
                "orgn": row.get("orgn_ntby_qty") or row.get("orgn_ntby_tr_pbmn") or "—",
                "indv": row.get("prsn_ntby_qty") or row.get("prsn_ntby_tr_pbmn") or "—",
            }
        return out or None
    except Exception as e:
        sys.stderr.write(f"[flow] 수급 실패: {e}\n")
        return None


# ----------------------------- 데이터 -----------------------------
def load_ticker():
    try:
        with open(TICKER, encoding="utf-8") as f:
            tk = json.load(f)
        by = {it["name"]: it for it in tk.get("items", [])}
        return by, tk.get("asof", "")
    except Exception:
        return {}, ""


def dirword(d):
    return "상승" if d == "up" else ("하락" if d == "down" else "보합")


def cls(d):
    return "up" if d == "up" else ("down" if d == "down" else "")


# ----------------------------- HTML -----------------------------
ART_CSS = """<style>
 :root{--navy:#1B3C6E;--gold:#C9A654;--gold-d:#a98731;--ink:#1F2933;--mute:#6B7785;--line:#E2E6EC;
  --up:#C0392B;--down:#1B5E9B;
  --serif:'Noto Serif KR',serif;--latin:'Playfair Display',serif;
  --sans:'Pretendard','Pretendard Variable',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;}
 *{box-sizing:border-box}
 body{margin:0;background:#F7F8FA;color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.72;-webkit-font-smoothing:antialiased}
 a{color:#1B5588}
 .topbar{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
 .topbar-in{max-width:820px;margin:0 auto;padding:12px 22px;display:flex;align-items:center;justify-content:space-between}
 .home{font-family:var(--latin);font-weight:800;color:var(--navy);letter-spacing:.1em;text-decoration:none;font-size:16px}
 .home:before{content:"\\2190 ";color:var(--gold-d);font-family:var(--sans)}
 .topbar .tag{font-size:11.5px;font-weight:700;color:#fff;background:var(--navy);padding:3px 10px;border-radius:2px}
 main{max-width:820px;margin:0 auto;padding:6px 22px 60px}
 .art-hero{background:var(--navy);color:#fff;border-radius:4px;padding:26px 26px 28px;margin:18px 0 22px}
 .art-hero .ah-k{color:var(--gold);font-weight:800;font-size:12px;letter-spacing:.12em;margin-bottom:8px}
 .art-hero .ah-t{font-family:var(--serif);font-weight:900;font-size:clamp(22px,4.2vw,30px);line-height:1.32}
 .art-hero .ah-s{color:#cdd9ea;font-size:13px;margin-top:10px;line-height:1.6}
 .sec{margin:34px 0 14px;border-top:2px solid var(--navy);padding-top:14px}
 .sec .eyebrow{color:var(--gold-d);font-weight:800;font-size:11.5px;letter-spacing:.1em;margin-bottom:4px}
 .sec h2{font-family:var(--serif);color:var(--navy);font-weight:900;font-size:clamp(19px,3.6vw,24px);margin:0;line-height:1.35}
 p{margin:0 0 13px}
 .lead-para{font-size:17px;line-height:1.85}
 .small{font-size:12.5px;color:var(--mute);line-height:1.6}
 .disc{font-size:11.5px;color:var(--mute);line-height:1.65}
 .up{color:var(--up);font-weight:700}
 .down{color:var(--down);font-weight:700}
 table.grid{width:100%;border-collapse:collapse;margin:6px 0 18px;font-size:13.5px;border:1px solid var(--line);table-layout:fixed}
 table.grid th{background:var(--navy);color:#fff;font-weight:700;text-align:left;padding:9px 10px;font-size:12.5px;vertical-align:top}
 table.grid td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:top;word-break:keep-all}
 table.grid tbody tr:nth-child(even){background:#F4F6F8}
 table.kv{width:100%;border-collapse:collapse;margin:4px 0 16px;font-size:13.5px;border:1px solid var(--line)}
 table.kv th{width:34%;text-align:left;background:#F2F4F6;color:var(--ink);font-weight:700;padding:8px 10px;border-top:1px solid var(--line);border-right:1px solid var(--line);vertical-align:top;word-break:keep-all}
 table.kv td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
 hr.rule{border:none;border-top:1px solid var(--line);margin:24px 0}
 .artfoot{max-width:820px;margin:0 auto;padding:22px;border-top:2px solid var(--navy);text-align:center}
 .artfoot a.kk{display:inline-block;background:#FEE500;color:#191600;font-weight:700;font-size:13.5px;padding:9px 16px;border-radius:3px;text-decoration:none;margin:6px 0 12px}
 .artfoot .back{display:inline-block;color:var(--navy);font-weight:700;text-decoration:none;font-size:13.5px}
 .artfoot .dom{color:var(--gold-d);font-weight:700;font-size:12px;letter-spacing:.06em;margin-top:10px}
 a:focus-visible{outline:3px solid var(--gold);outline-offset:2px}
</style>"""

HEAD_A = ('<!doctype html><html lang="ko"><head>\n'
          '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
          '<title>')
HEAD_B = (' · INVEST STORY</title>\n'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
          '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=Noto+Serif+KR:wght@600;700;900&display=swap" rel="stylesheet">\n'
          '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">\n'
          + ART_CSS + '\n</head><body>\n')


def head_html(title_esc):
    return HEAD_A + title_esc + HEAD_B

FOOT = ('<footer class="artfoot">\n'
        ' <a class="kk" href="https://open.kakao.com/o/giw7dfAb" target="_blank" rel="noopener">투자이야기 오픈채팅 바로가기</a><br>\n'
        ' <a class="back" href="/">\u2190 다른 리포트 보러가기</a>\n'
        ' <div class="dom">investstory.co.kr</div>\n'
        '</footer>\n</body></html>')


def index_table(items, header):
    order = ["KOSPI", "KOSDAQ", "USD/KRW", "WTI", "S&P 500", "나스닥", "달러인덱스"]
    rows = []
    for nm in order:
        it = items.get(nm)
        if not it:
            continue
        c = cls(it.get("dir", ""))
        rows.append(f'<tr><td>{esc(nm)}</td><td>{esc(it.get("value","—"))}</td>'
                    f'<td class="{c}">{esc(it.get("change","—"))}</td></tr>')
    if not rows:
        return ""
    return (f'<table class="grid"><colgroup><col style="width:40%"><col style="width:32%"><col style="width:28%"></colgroup>'
            f'<thead><tr><th>{esc(header)}</th><th>지수/가격</th><th>등락</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def rank_table(rows, title):
    if not rows:
        return ""
    trs = []
    for r in rows:
        up = str(r.get("sign", "3")) in ("1", "2")
        c = "up" if up else "down"
        trs.append(f'<tr><td>{esc(r["name"])}</td><td>{esc(r["price"])}</td>'
                   f'<td class="{c}">{esc(r["ctrt"])}%</td></tr>')
    return (f'<div class="src-cat">{esc(title)}</div>'
            f'<table class="grid"><colgroup><col style="width:46%"><col style="width:30%"><col style="width:24%"></colgroup>'
            f'<thead><tr><th>종목</th><th>현재가</th><th>등락률</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>')


def flow_table(flows):
    if not flows:
        return ""
    trs = []
    for mkt, v in flows.items():
        trs.append(f'<tr><td>{esc(mkt)}</td><td>{esc(v.get("frgn","—"))}</td>'
                   f'<td>{esc(v.get("orgn","—"))}</td><td>{esc(v.get("indv","—"))}</td></tr>')
    return ('<table class="grid"><thead><tr><th>시장</th><th>외국인</th><th>기관</th><th>개인</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>'
            '<p class="small">순매수 기준(단위는 KIS 응답 기준). 잠정치이며 장 마감 후 확정치와 다를 수 있습니다.</p>')


def lead_sentence_close(items):
    parts = []
    for nm in ("KOSPI", "KOSDAQ"):
        it = items.get(nm)
        if it:
            parts.append(f'{nm.replace("KOSPI","코스피").replace("KOSDAQ","코스닥")}는 '
                         f'전 거래일 대비 {esc(it.get("change",""))} {dirword(it.get("dir"))}한 '
                         f'{esc(it.get("value",""))}로 마감했습니다.')
    fx = items.get("USD/KRW")
    if fx:
        parts.append(f'원/달러 환율은 {esc(fx.get("value",""))}원({esc(fx.get("change",""))})을 기록했습니다.')
    return " ".join(parts) or "주요 지수 마감 데이터를 집계했습니다."


def lead_sentence_open(items):
    parts = ["간밤 미국 증시와 환율·유가 흐름을 정리한 개장 브리핑입니다."]
    us = []
    for nm in ("S&P 500", "나스닥"):
        it = items.get(nm)
        if it:
            us.append(f'{nm} {esc(it.get("value",""))}({esc(it.get("change",""))})')
    if us:
        parts.append("간밤 " + ", ".join(us) + ".")
    wti = items.get("WTI"); fx = items.get("USD/KRW")
    tail = []
    if wti: tail.append(f'WTI {esc(wti.get("value",""))}({esc(wti.get("change",""))})')
    if fx:  tail.append(f'원/달러 {esc(fx.get("value",""))}원({esc(fx.get("change",""))})')
    if tail:
        parts.append(" · ".join(tail) + ".")
    return " ".join(parts)


def build_html(mode, date_kst, items, asof, rank_up, rank_dn, flows):
    d = date_kst
    dstr = f'{d.year}년 {d.month}월 {d.day}일({WEEKDAY_KR[d.weekday()]})'
    kospi = items.get("KOSPI", {})
    if mode == "close":
        eyebrow = "JOSH PARK INVEST · 데일리 마감 시황"
        title = f'{d.month}월 {d.day}일 마감 — 코스피 {esc(kospi.get("value","—"))} ({esc(kospi.get("change","—"))})'
        hero_sub = lead_sentence_close(items)
        lead = lead_sentence_close(items)
        body = [f'<p class="lead-para">{lead}</p>']
        body.append('<section class="sec"><div class="eyebrow">CLOSING · 지수 마감</div><h2>지수 마감 현황</h2></section>')
        body.append(index_table(items, "지수 (종가/현재)"))
        if rank_up or rank_dn:
            body.append('<section class="sec"><div class="eyebrow">MOVERS · 등락 상위</div><h2>오늘의 등락 상위 종목</h2></section>')
            body.append(rank_table(rank_up, "상승률 상위"))
            body.append(rank_table(rank_dn, "하락률 상위"))
        if flows:
            body.append('<section class="sec"><div class="eyebrow">FLOW · 투자자 수급</div><h2>외국인·기관 수급</h2></section>')
            body.append(flow_table(flows))
        disc = (f'본 리포트는 시장 정보 제공 목적이며 특정 종목의 매수·매도 권유가 아닙니다. '
                f'수치는 작성 시점({esc(asof) or dstr} 기준) 데이터로, 장 마감 직후 잠정치가 포함될 수 있어 '
                f'한국거래소 확정치와 차이가 있을 수 있습니다. 모든 투자 결정은 투자자 본인의 판단과 책임 하에 '
                f'이루어져야 하며, Josh Park Invest는 본 자료를 활용한 투자 결과에 어떠한 책임도 지지 않습니다.')
        tag = "마감"
    else:
        eyebrow = "JOSH PARK INVEST · 데일리 개장 브리핑"
        title = f'{d.month}월 {d.day}일 개장 브리핑 — 간밤 미국증시·환율 점검'
        hero_sub = lead_sentence_open(items)
        lead = lead_sentence_open(items)
        body = [f'<p class="lead-para">{lead}</p>']
        body.append('<section class="sec"><div class="eyebrow">PRE-MARKET · 간밤 글로벌</div><h2>간밤 미국증시·환율·유가</h2></section>')
        body.append(index_table(items, "지표 (직전 확인값)"))
        body.append('<p class="small">위 미국 지수·환율·유가는 한국 개장 직전 확인값 기준입니다. 코스피·코스닥은 전 거래일 종가입니다.</p>')
        disc = (f'본 리포트는 개장 전 참고용 정보 제공 목적이며 매수·매도 권유가 아닙니다. 수치는 작성 시점 '
                f'직전 확인값으로 실시간과 차이가 있을 수 있습니다. 모든 투자 결정은 투자자 본인의 판단과 책임 '
                f'하에 이루어져야 하며, Josh Park Invest는 본 자료를 활용한 투자 결과에 책임을 지지 않습니다.')
        tag = "개장"

    parts = [head_html(esc(title))]
    parts.append(f'<div class="topbar"><div class="topbar-in"><a class="home" href="/">INVEST STORY</a>'
                 f'<span class="tag">데일리 · {tag} · {d.strftime("%Y-%m-%d")}</span></div></div>\n')
    parts.append('<main>\n')
    parts.append(f'<header class="art-hero"><div class="ah-k">{esc(eyebrow)}</div>'
                 f'<div class="ah-t">{esc(title)}</div><div class="ah-s">{esc(dstr)} · {esc(hero_sub)}</div></header>\n')
    parts.extend(body)
    parts.append(f'<hr class="rule"><p class="disc">{disc}</p>')
    parts.append('<p class="byline" style="margin:30px 0 4px;padding-top:16px;border-top:1px solid var(--line);'
                 'font-weight:700;color:var(--ink);font-size:14px">박철웅 기자 '
                 '<a href="mailto:joshpark.korea@gmail.com" style="font-weight:600">joshpark.korea@gmail.com</a></p>\n')
    parts.append('</main>\n')
    parts.append(FOOT)
    summary = (hero_sub[:180])
    return "".join(parts), title, summary


# ----------------------------- manifest / build -----------------------------
def update_manifest(date_str, mode, title, summary, relfile):
    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    issues = man.setdefault("issues", [])
    # 같은 파일(=같은 날짜·모드)이면 갱신
    existing = next((it for it in issues if it.get("file") == relfile), None)
    if existing:
        existing.update({"date": date_str, "tag": "데일리", "title": title, "summary": summary})
        no = existing.get("no")
    else:
        no = max([it.get("no", 0) for it in issues] + [0]) + 1
        issues.append({"no": no, "date": date_str, "tag": "데일리",
                       "title": title, "summary": summary, "file": relfile})
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    return no


def rebuild_site():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_site.py")],
                       cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
    return r.returncode == 0


# ----------------------------- selftest -----------------------------
def selftest(token, key, sec):
    print("=== daily_news 자가진단 ===")
    print("KIS 키:", "있음" if (key and sec) else "없음")
    print("토큰:", "발급 성공" if token else "발급 실패/없음")
    items, asof = load_ticker()
    print("ticker.json:", f"{len(items)}종 수신" if items else "없음/실패", "| asof:", asof)
    if token:
        now = datetime.now(KST)
        print("오늘 거래일?:", trading_day(now, token, key, sec))
        up = fetch_fluctuation(token, key, sec, "0", 3)
        print("등락률 상위:", "OK " + ", ".join(x["name"] for x in up) if up else "실패(섹션 생략됨)")
        dn = fetch_fluctuation(token, key, sec, "1", 3)
        print("등락률 하위:", "OK" if dn else "실패(섹션 생략됨)")
        fl = fetch_investors(token, key, sec)
        print("수급(외국인/기관):", "OK" if fl else "실패(섹션 생략됨)")
    print("=== 끝 (파일은 만들지 않았습니다) ===")


# ----------------------------- main -----------------------------
def main():
    args = [a for a in sys.argv[1:]]
    mode = "close"
    for a in args:
        if a in ("open", "close"):
            mode = a
    force = "--force" in args
    do_selftest = "--selftest" in args

    key = os.environ.get("KIS_APP_KEY", "").strip()
    sec = os.environ.get("KIS_APP_SECRET", "").strip()
    token = None
    if key and sec:
        try:
            token = kis_token(key, sec)
        except Exception as e:
            sys.stderr.write(f"[kis] 토큰 발급 실패: {e}\n")

    if do_selftest:
        selftest(token, key, sec)
        return 0

    now = datetime.now(KST)
    ok, why = (True, "강제") if force else trading_day(now, token, key, sec)
    print(f"[daily_news] {now:%Y-%m-%d %H:%M KST} · mode={mode} · 거래일판정={ok}({why})")
    if not ok:
        print("[daily_news] 휴장일 — 생성하지 않고 종료")
        return 0

    items, asof = load_ticker()
    if not items:
        sys.stderr.write("[daily_news] ticker.json 비어있음 — update_ticker.py를 먼저 실행하세요\n")
        return 1

    rank_up = rank_dn = flows = None
    if mode == "close" and token:
        rank_up = fetch_fluctuation(token, key, sec, "0", 5)
        rank_dn = fetch_fluctuation(token, key, sec, "1", 5)
        flows = fetch_investors(token, key, sec)
        print(f"[daily_news] 등락상위={'O' if rank_up else 'X'} 등락하위={'O' if rank_dn else 'X'} 수급={'O' if flows else 'X'}")

    htmlstr, title, summary = build_html(mode, now, items, asof, rank_up, rank_dn, flows)

    os.makedirs(NEWS_DIR, exist_ok=True)
    fname = f'{now:%Y-%m-%d}-{mode}.html'
    relfile = f'newsletters/{fname}'
    with open(os.path.join(NEWS_DIR, fname), "w", encoding="utf-8") as f:
        f.write(htmlstr)
    print(f"[daily_news] 기사 작성: {relfile}")

    no = update_manifest(now.strftime("%Y-%m-%d"), mode, title, summary, relfile)
    print(f"[daily_news] manifest 등록: 제{no}호 (데일리)")

    if rebuild_site():
        print("[daily_news] index.html 재생성 완료")
    else:
        sys.stderr.write("[daily_news] build_site 실패\n"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
