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
import re
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_DIR = os.path.join(ROOT, "newsletters")
MANIFEST = os.path.join(ROOT, "manifest.json")
KIMCHI_HISTORY = os.path.join(ROOT, "data", "kimchi_history.json")
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
    """접근토큰: 공용 캐시(.cache/kis_token.json)에서 재사용, 없으면 1회 발급(+캐시).
    update_ticker는 발급하지 않으므로, 실제 발급은 매일 9:05 개장 실행 때 여기서만 일어난다
    → 한투 '1일 1회 발급 원칙' 준수. 발급 시 1분당 1회 제한은 kis_auth가 65초 재시도로 회피."""
    import kis_auth
    return kis_auth.get_token(app_key, app_secret, allow_issue=True)


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


def fetch_topcap_codes(token, app_key, app_secret, n=100):
    """시가총액 상위 n위 종목코드 집합 — 등락률 상하위 필터용. 실패 시 None."""
    try:
        params = {
            "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20174",
            "fid_input_iscd": "0000", "fid_div_cls_code": "0",
            "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0",
            "fid_input_price_1": "", "fid_input_price_2": "", "fid_vol_cnt": "",
        }
        data = kis_get("/uapi/domestic-stock/v1/ranking/market-cap", "FHPST01740000",
                       params, token, app_key, app_secret)
        if data.get("rt_cd") != "0":
            return None
        codes = set()
        for o in data.get("output", [])[:n]:
            c = o.get("mksc_shrn_iscd") or o.get("stck_shrn_iscd") or ""
            if c:
                codes.add(c)
        return codes or None
    except Exception as e:
        sys.stderr.write(f"[topcap] 시총상위 실패: {e}\n")
        return None


def fetch_fluctuation(token, app_key, app_secret, sort_code, n=5, allow=None):
    """등락률 순위. sort_code: '0'=상승률 상위, '1'=하락률 상위. 실패 시 None.
    allow가 주어지면(시총 상위 코드집합) 그 안의 종목만 채택."""
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
        for o in data.get("output", []):
            code = o.get("stck_shrn_iscd") or o.get("mksc_shrn_iscd") or ""
            if allow is not None and code not in allow:
                continue
            out.append({
                "name": o.get("hts_kor_isnm", "—"),
                "code": code,
                "price": o.get("stck_prpr", "—"),
                "ctrt": o.get("prdy_ctrt", "—"),   # 등락률(%)
                "sign": o.get("prdy_vrss_sign", "3"),
            })
            if len(out) >= n:
                break
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


def fetch_value_rank(token, app_key, app_secret, n=5):
    """국내 거래대금(거래금액) 상위 종목 — best-effort. 저가주 쏠림 방지 위해 거래량 대신 거래대금 기준. 실패 시 None."""
    try:
        params = {
            "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20171",
            "fid_input_iscd": "0000", "fid_div_cls_code": "0", "fid_blng_cls_code": "3",  # 3=거래금액(거래대금)순
            "fid_trgt_cls_code": "111111111", "fid_trgt_exls_cls_code": "0000000000",
            "fid_input_price_1": "", "fid_input_price_2": "", "fid_vol_cnt": "",
            "fid_input_date_1": "",
        }
        data = kis_get("/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000",
                       params, token, app_key, app_secret)
        if data.get("rt_cd") != "0":
            return None
        out = []
        for o in data.get("output", [])[:n]:
            out.append({
                "name": o.get("hts_kor_isnm", "—"),
                "code": o.get("mksc_shrn_iscd") or o.get("stck_shrn_iscd") or "",
                "price": o.get("stck_prpr", "—"),
                "ctrt": o.get("prdy_ctrt", "—"),
                "vol": o.get("acml_vol", "—"),
                "val": o.get("acml_tr_pbmn", "—"),   # 누적 거래대금
                "sign": o.get("prdy_vrss_sign", "3"),
            })
        return out or None
    except Exception as e:
        sys.stderr.write(f"[val] 거래대금순위 실패: {e}\n")
        return None


# --------------------- 종목 핵심지표(전일종가/금일종가/거래량/시가총액) ---------------------
# AI는 표를 직접 쓰지 않고 본문에 {{STOCK|시장|식별자|종목명}} 토큰만 남긴다.
# 그 자리에 코드가 KIS(한국)/FMP(미국)로 실제 숫자를 받아 '항상 동일한 4줄 표'를 박는다.
FMP_BASE = "https://financialmodelingprep.com/api/v3"

def _ci(n):
    try:
        return f"{int(round(float(n))):,}"
    except Exception:
        return None

def _mcap_usd(raw):
    try:
        v = float(raw)
    except Exception:
        return None
    if v <= 0:
        return None
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.1f}B"
    if v >= 1e6:  return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def _mcap_kr(eok):
    try:
        v = float(eok)
    except Exception:
        return None
    if v <= 0:
        return None
    jo = int(v // 10000); rem = int(round(v % 10000))
    if jo and rem: return f"{jo:,}조 {rem:,}억원"
    if jo:         return f"{jo:,}조원"
    return f"{rem:,}억원"

_CHGCOLOR = {"up": "#C0392B", "down": "#1B5E9B", "flat": "#1a1a1a"}  # 상승 빨강 / 하락 파랑 / 보합 검정

def _pct_disp(rate):
    """부호 있는 실수(%) → ('+10.64%'|'-0.37%'|'0.00%', 'up'|'down'|'flat'). 실패 시 (None,'flat')."""
    try:
        v = float(rate)
    except Exception:
        return (None, "flat")
    if v > 0:
        return (f"+{v:.2f}%", "up")
    if v < 0:
        return (f"{v:.2f}%", "down")
    return ("0.00%", "flat")


def _fmp_get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return json.load(r)

def fmp_quote(ticker, fmp_key):
    """미국 종목 → {pc,c,vol,mcap}. FMP stable /quote 우선, 실패 시 구버전 v3, 그래도 안 되면 None."""
    if not fmp_key:
        sys.stderr.write("[fmp] FMP_API_KEY 비어있음 — Actions 시크릿 확인 필요\n")
        return None
    t = urllib.parse.quote(ticker); k = urllib.parse.quote(fmp_key)
    urls = [
        f"https://financialmodelingprep.com/stable/quote?symbol={t}&apikey={k}",
        f"https://financialmodelingprep.com/api/v3/quote/{t}?apikey={k}",
    ]
    for u in urls:
        try:
            arr = _fmp_get(u)
            if isinstance(arr, dict):
                if arr.get("Error Message") or arr.get("error"):
                    sys.stderr.write(f"[fmp] {ticker} API응답오류: {str(arr)[:140]}\n")
                    continue
                arr = [arr]
            if not arr:
                continue
            q = arr[0]
            pc, c, vol, mc = q.get("previousClose"), q.get("price"), q.get("volume"), q.get("marketCap")
            if c is None and pc is None:
                continue
            chg_disp, chg_dir = _pct_disp(q.get("changePercentage", q.get("changesPercentage")))
            return {
                "pc":   (f"${float(pc):,.2f}" if pc is not None else "—"),
                "c":    (f"${float(c):,.2f}"  if c  is not None else "—"),
                "chg":  chg_disp, "dir": chg_dir,
                "vol":  ((_ci(vol) + "주") if _ci(vol) else "—"),
                "mcap": (_mcap_usd(mc) or "—"),
            }
        except Exception as e:
            code = getattr(e, "code", None)
            sys.stderr.write(f"[fmp] {ticker} 실패{(' HTTP '+str(code)) if code else ''}: {e}\n")
    return None

def twelvedata_quote(symbol, td_key):
    """FMP가 빈값을 준 미국 종목의 2차 폴백.
    Twelve Data quote → {pc,c,chg,dir,vol,mcap}. 시가총액은 무료 플랜 미제공이라 '—'."""
    if not td_key:
        return None
    try:
        sym = urllib.parse.quote(symbol.strip().upper())
        url = (f"https://api.twelvedata.com/quote?symbol={sym}"
               f"&apikey={urllib.parse.quote(td_key)}")
        j = _fmp_get(url)  # 동일한 JSON GET 헬퍼 재사용
    except Exception as e:
        sys.stderr.write(f"[td] {symbol} 호출 실패: {e}\n")
        return None
    if not isinstance(j, dict) or j.get("status") == "error" or j.get("code"):
        sys.stderr.write(f"[td] {symbol} 응답오류: {str(j)[:140]}\n")
        return None
    pc, c, vol = j.get("previous_close"), j.get("close"), j.get("volume")
    if c is None and pc is None:
        return None
    def _usd(x):
        try:
            return f"${float(x):,.2f}"
        except Exception:
            return "—"
    chg_disp, chg_dir = _pct_disp(j.get("percent_change"))
    return {
        "pc":   (_usd(pc) if pc is not None else "—"),
        "c":    (_usd(c)  if c  is not None else "—"),
        "chg":  chg_disp, "dir": chg_dir,
        "vol":  ((_ci(vol) + "주") if _ci(vol) else "—"),
        "mcap": "—",  # Twelve Data 무료: 시가총액 미제공 → 그 칸만 '—'
    }


def fmp_index(symbol, fmp_key):
    """지수(예: ^DJI) → {value,change,dir}. 실패 시 None."""
    if not fmp_key:
        return None
    t = urllib.parse.quote(symbol); k = urllib.parse.quote(fmp_key)
    for u in (f"https://financialmodelingprep.com/stable/quote?symbol={t}&apikey={k}",
              f"https://financialmodelingprep.com/api/v3/quote/{t}?apikey={k}"):
        try:
            arr = _fmp_get(u)
            if isinstance(arr, dict):
                arr = [arr]
            if not arr:
                continue
            q = arr[0]
            price = q.get("price")
            chg = q.get("changePercentage", q.get("changesPercentage"))
            if price is None:
                continue
            c = float(chg) if chg is not None else 0.0
            return {"value": f"{float(price):,.2f}",
                    "change": f"{'+' if c >= 0 else ''}{c:.2f}%",
                    "dir": "up" if c >= 0 else "down"}
        except Exception as e:
            sys.stderr.write(f"[fmp-index] {symbol} 실패: {e}\n")
    return None


# ---------------- 무료(야후) 미국 데이터 경로 — FMP 유료화 대응(2026-07-07) ----------------
# FMP Free 플랜이 quote/most-actives/news를 402(Payment Required)로 막으면서 신설.
# 야후 파이낸스 공개 엔드포인트(키·인증 불필요)를 폴백으로 사용한다. 호출 순서는
# 항상 FMP 먼저 → 실패 시 야후. (추후 FMP 유료 전환 시 자동으로 FMP 경로 복귀)

def _yahoo_get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=20) as r:
        return json.load(r)

def yahoo_chart_quote(sym):
    """야후 v8 chart(무키) → {'price','prev','vol','pct'} 또는 None."""
    try:
        j = _yahoo_get(f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1d&interval=1d")
        meta = ((j.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        vol = meta.get("regularMarketVolume")
        if price is None:
            return None
        pct = ((float(price) / float(prev)) - 1.0) * 100.0 if prev else None
        return {"price": float(price), "prev": (float(prev) if prev else None),
                "vol": (float(vol) if vol else 0.0), "pct": pct}
    except Exception as e:
        sys.stderr.write(f"[yh] {sym} chart 실패: {e}\n")
        return None

def yahoo_index(symbol):
    """지수(예: ^DJI) → {value,change,dir}. fmp_index와 동일 형태. 실패 시 None."""
    q = yahoo_chart_quote(symbol)
    if not q or q.get("pct") is None:
        return None
    c = q["pct"]
    return {"value": f"{q['price']:,.2f}",
            "change": f"{'+' if c >= 0 else ''}{c:.2f}%",
            "dir": "up" if c >= 0 else "down"}

def yahoo_stock_fallback(sym):
    """개별 미국 종목 3차 폴백 → std_stock_table용 dict. 시가총액은 미제공 '—'."""
    q = yahoo_chart_quote(sym)
    if not q:
        return None
    chg_disp, chg_dir = _pct_disp(q.get("pct"))
    return {"pc": (f"${q['prev']:,.2f}" if q.get("prev") else "—"),
            "c": f"${q['price']:,.2f}",
            "chg": chg_disp, "dir": chg_dir,
            "vol": ((_ci(q.get("vol")) + "주") if _ci(q.get("vol")) else "—"),
            "mcap": "—"}

def fetch_us_yields():
    """미국 국채금리(^TNX 10년물, ^TYX 30년물) — 야후 무키 경로. (독자 피드백 반영, 2026-07-07)
    야후 금리 지수는 수익률×10 관례가 혼재하므로 20 초과 시 10으로 나눠 정규화.
    반환: {'미국 10년물 금리': {value,change,dir}, ...} · 실패 항목은 생략(비치명)."""
    out = {}
    for sym, name in (("^TNX", "미국 10년물 금리"), ("^TYX", "미국 30년물 금리")):
        q = yahoo_chart_quote(sym)
        if not q or q.get("prev") is None:
            continue
        v, pv = q["price"], q["prev"]
        if v > 20: v /= 10.0
        if pv > 20: pv /= 10.0
        d = v - pv
        out[name] = {"value": f"{v:.2f}%",
                     "change": f"{'+' if d >= 0 else ''}{d:.2f}%p",
                     "dir": "up" if d >= 0 else "down"}
    if out:
        print("[yh] 국채금리 확보: " + ", ".join(f"{k} {v['value']}({v['change']})" for k, v in out.items()))
    return out


# 무료 경로 후보 유니버스 — 미국 대형·초대형 유동성 상위 40종목(거래대금 상위권 상시 커버).
# 시총 필터를 API 없이 대신하는 장치이므로, 신규 대형주 등장 시 여기에 추가할 것.
US_UNIVERSE = [
    ("NVDA","엔비디아"),("TSLA","테슬라"),("AAPL","애플"),("MSFT","마이크로소프트"),
    ("AMZN","아마존"),("META","메타"),("GOOGL","알파벳"),("AVGO","브로드컴"),
    ("AMD","AMD"),("MU","마이크론"),("INTC","인텔"),("PLTR","팔란티어"),
    ("SMCI","슈퍼마이크로"),("QCOM","퀄컴"),("ARM","Arm"),("TSM","TSMC"),
    ("COIN","코인베이스"),("MSTR","스트래티지"),("LRCX","램리서치"),("AMAT","어플라이드머티어리얼즈"),
    ("KLAC","KLA"),("WDC","웨스턴디지털"),("STX","씨게이트"),("SNDK","샌디스크"),
    ("DELL","델"),("ORCL","오라클"),("NFLX","넷플릭스"),("CRM","세일즈포스"),
    ("PANW","팔로알토네트웍스"),("NOW","서비스나우"),("UBER","우버"),("HOOD","로빈후드"),
    ("JPM","JP모건"),("XOM","엑슨모빌"),("LLY","일라이릴리"),("UNH","유나이티드헬스"),
    ("WMT","월마트"),("COST","코스트코"),("VRT","버티브"),("CEG","컨스텔레이션에너지"),
]
US_KR_NAMES = dict(US_UNIVERSE)

def fetch_us_movers_free(n=5):
    """무료 경로: 유니버스 40종목을 야후 차트로 훑어 거래대금(주가×거래량) 상위 n 반환.
    유니버스 자체가 대형주라 페니·소형주 필터가 내장된 셈. 실패 시 None."""
    rows = []
    for i, (sym, kr) in enumerate(US_UNIVERSE):
        if i:
            time.sleep(0.12)
        q = yahoo_chart_quote(sym)
        if not q or q["price"] < 5.0:
            continue
        val = q["price"] * (q.get("vol") or 0.0)
        if val <= 0:
            continue
        chg, _ = _pct_disp(q.get("pct"))
        rows.append({"symbol": sym, "name": kr,
                     "price": f"${q['price']:,.2f}", "chg": chg, "_val": val})
    if not rows:
        return None
    rows.sort(key=lambda r: r["_val"], reverse=True)
    print(f"[yh] 무료 경로 거래대금 상위: {', '.join(r['symbol'] for r in rows[:n])}")
    return [{k: v for k, v in r.items() if k != "_val"} for r in rows[:n]]

def fetch_trending_us():
    """무료 뉴스픽 대체: 야후 트렌딩(US) 상위 티커 중 대형·유동성 조건
    (주가 $5+ · 당일 거래대금 $10억+) 통과 첫 종목. 실패 시 None."""
    try:
        j = _yahoo_get("https://query1.finance.yahoo.com/v1/finance/trending/US?count=10")
        quotes = ((j.get("finance") or {}).get("result") or [{}])[0].get("quotes") or []
        syms = [str(q.get("symbol") or "").strip().upper() for q in quotes]
        syms = [s for s in syms if s and s.isalpha() and 1 <= len(s) <= 5]
    except Exception as e:
        sys.stderr.write(f"[yh] 트렌딩 조회 실패: {e}\n")
        return None
    for i, sym in enumerate(syms[:6]):
        if i:
            time.sleep(0.12)
        q = yahoo_chart_quote(sym)
        if not q or q["price"] < 5.0:
            continue
        if q["price"] * (q.get("vol") or 0.0) < 1.0e9:
            continue
        chg, _ = _pct_disp(q.get("pct"))
        print(f"[yh] 화제성 픽(야후 트렌딩): {sym}")
        return {"symbol": sym, "name": US_KR_NAMES.get(sym, ""),
                "price": f"${q['price']:,.2f}", "chg": chg, "news": True}
    return None

def kis_stock(code, token, key, sec):

    """한국 종목 → {pc,c,vol,mcap}. 실패 시 None. (inquire-price FHKST01010100)"""
    if not token or not code:
        return None
    try:
        data = kis_get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                       {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
                       token, key, sec)
        if data.get("rt_cd") != "0":
            return None
        o = data.get("output") or {}
        c  = o.get("stck_prpr")    # 현재가(=금일 종가)
        pc = o.get("stck_sdpr")    # 기준가(=전일 종가)
        vol = o.get("acml_vol")    # 누적거래량
        mc = o.get("hts_avls")     # 시가총액(억원)
        sign = o.get("prdy_vrss_sign", "3")   # 1·2 상승 / 4·5 하락 / 3 보합
        try:
            mag = abs(float(o.get("prdy_ctrt", "0")))
            signed = mag if sign in ("1", "2") else (-mag if sign in ("4", "5") else 0.0)
            chg_disp, chg_dir = _pct_disp(signed)
        except Exception:
            chg_disp, chg_dir = (None, "flat")
        return {
            "pc":   ((_ci(pc) + "원") if _ci(pc) else "—"),
            "c":    ((_ci(c) + "원") if _ci(c) else "—"),
            "chg":  chg_disp, "dir": chg_dir,
            "vol":  ((_ci(vol) + "주") if _ci(vol) else "—"),
            "mcap": (_mcap_kr(mc) or "—"),
        }
    except Exception as e:
        sys.stderr.write(f"[kis-stock] {code} 실패: {e}\n")
        return None

def yahoo_kr_stock_fallback(code):
    """국내 종목 2차 폴백(KIS 장애 대비, 2026-07-07 신설) — 야후 {code}.KS→.KQ 순차 시도.
    kis_stock과 동일 형태 {pc,c,chg,dir,vol,mcap} 반환. 시가총액은 미제공 '—'."""
    for suf in (".KS", ".KQ"):
        q = yahoo_chart_quote(f"{code}{suf}")
        if q and q.get("prev"):
            chg_disp, chg_dir = _pct_disp(((q["price"] / q["prev"]) - 1.0) * 100.0)
            print(f"[yh] 국내 {code}{suf} 폴백 성공(시가총액 제외)")
            return {"pc": (_ci(q["prev"]) + "원") if _ci(q["prev"]) else "—",
                    "c": (_ci(q["price"]) + "원") if _ci(q["price"]) else "—",
                    "chg": chg_disp, "dir": chg_dir,
                    "vol": ((_ci(q.get("vol")) + "주") if _ci(q.get("vol")) else "—"),
                    "mcap": "—"}
    return None


def std_stock_table(name, ident, d):
    """전일 종가 / 금일 종가(+등락률) / 거래량 / 시가총액 — 모든 종목 동일한 4줄 표."""
    if d is None:
        d = {"pc": "—", "c": "—", "vol": "—", "mcap": "—"}
    c_cell = esc(d["c"])
    chg = d.get("chg")
    if chg:
        col = _CHGCOLOR.get(d.get("dir", "flat"), "#1a1a1a")
        c_cell += f' <span style="color:{col};font-weight:600">({esc(chg)})</span>'
    head = f'<div class="src-cat">{esc(name)} ({esc(ident)}) — 핵심 지표</div>' if name else ""
    return (head +
        '<table class="grid"><thead><tr><th>항목</th><th>수치</th></tr></thead><tbody>'
        f'<tr><td>전일 종가</td><td>{esc(d["pc"])}</td></tr>'
        f'<tr><td>금일 종가</td><td>{c_cell}</td></tr>'
        f'<tr><td>거래량</td><td>{esc(d["vol"])}</td></tr>'
        f'<tr><td>시가총액</td><td>{esc(d["mcap"])}</td></tr>'
        '</tbody></table>')

def _fmp_fmt(q):
    """FMP quote dict → {pc,c,chg,dir,vol,mcap} 표시문자열."""
    pc, c, vol, mc = q.get("previousClose"), q.get("price"), q.get("volume"), q.get("marketCap")
    chg_disp, chg_dir = _pct_disp(q.get("changePercentage", q.get("changesPercentage")))
    return {
        "pc":   (f"${float(pc):,.2f}" if pc is not None else "—"),
        "c":    (f"${float(c):,.2f}"  if c  is not None else "—"),
        "chg":  chg_disp, "dir": chg_dir,
        "vol":  ((_ci(vol) + "주") if _ci(vol) else "—"),
        "mcap": (_mcap_usd(mc) or "—"),
    }

def _fmp_try(url):
    """URL 1회 호출 → quote dict 리스트(실패/오류 시 빈 리스트)."""
    try:
        arr = _fmp_get(url)
        if isinstance(arr, dict):
            if arr.get("Error Message") or arr.get("error"):
                sys.stderr.write(f"[fmp] 응답오류: {str(arr)[:140]}\n")
                return []
            arr = [arr]
        return arr or []
    except Exception as e:
        code = getattr(e, "code", None)
        tail = url.split("?")[0].rsplit("/", 1)[-1]
        sys.stderr.write(f"[fmp] {tail} 실패{(' HTTP ' + str(code)) if code else ''}: {e}\n")
        return []

def fmp_quotes(tickers, fmp_key):
    """여러 미국 티커 → {TICKER: {...}}. 배치로 한 번에 시도하고,
    빠진 종목은 단일 호출로 보강(분당/버스트 제한 회피: 호출 간격 + 실패 시 대기 후 재시도)."""
    out = {}
    syms = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not fmp_key:
        sys.stderr.write("[fmp] FMP_API_KEY 비어있음 — Actions 시크릿 확인 필요\n")
        return out
    if not syms:
        return out
    k = urllib.parse.quote(fmp_key)
    js = urllib.parse.quote(",".join(syms), safe=",")
    # 1) 배치 빠른 경로(되면 1회로 끝, 무료에서 막히면 빈 응답 → 폴백)
    for u in (f"https://financialmodelingprep.com/stable/batch-quote?symbols={js}&apikey={k}",
              f"https://financialmodelingprep.com/stable/quote?symbol={js}&apikey={k}"):
        for q in _fmp_try(u):
            sym = (q.get("symbol") or "").upper()
            if sym and sym not in out:
                out[sym] = _fmp_fmt(q)
        if all(s in out for s in syms):
            return out
    # 2) 빠진 종목은 단일 호출(검증된 엔드포인트). 간격 2초 + 실패 시 20초 후 1회 재시도.
    miss = [s for s in syms if s not in out]
    if miss:
        print(f"[fmp] 배치 미해결 {len(miss)}종목 단일 호출 보강: {', '.join(miss)}")
    for i, sym in enumerate(miss):
        if i:
            time.sleep(2)
        one = f"https://financialmodelingprep.com/stable/quote?symbol={urllib.parse.quote(sym)}&apikey={k}"
        arr = _fmp_try(one)
        if not arr:
            time.sleep(20)  # 분당 한도 리셋 대기 후 재시도
            arr = _fmp_try(one)
        if arr:
            out[sym] = _fmp_fmt(arr[0])
    return out

def fetch_us_movers(fmp_key, n=5):
    """간밤 미국장 '거래대금(주가×거래량)' 상위 대형주 — 페니주/소형주 배제. 실패 시 None.
    most-actives(거래활발)로 후보를 받은 뒤 quote로 거래량·시총을 확보해 거래대금 기준 재정렬한다."""
    if not fmp_key:
        return None
    k = urllib.parse.quote(fmp_key)
    # 1) 후보군: most-actives(거래활발 상위) — 최대 40종목
    cands = []
    for u in (f"https://financialmodelingprep.com/stable/most-actives?apikey={k}",
              f"https://financialmodelingprep.com/api/v3/stock_market/actives?apikey={k}"):
        try:
            arr = _fmp_get(u)
            if isinstance(arr, dict):
                if arr.get("Error Message") or arr.get("error"):
                    continue
                arr = arr.get("mostActiveStock") or arr.get("data") or []
            if not arr:
                continue
            for q in arr[:40]:
                sym = q.get("symbol") or q.get("ticker") or ""
                if sym and sym not in cands:
                    cands.append(sym)
            if cands:
                break
        except Exception as e:
            sys.stderr.write(f"[us_movers] most-actives 실패: {e}\n")
    if not cands:
        return None
    # 2) 후보 배치 시세(quote)로 거래량·시총 확보 → 거래대금 계산
    syms = urllib.parse.quote(",".join(cands[:40]))
    quotes = None
    for u in (f"https://financialmodelingprep.com/stable/quote?symbol={syms}&apikey={k}",
              f"https://financialmodelingprep.com/api/v3/quote/{syms}?apikey={k}"):
        try:
            quotes = _fmp_get(u)
            if quotes:
                break
        except Exception as e:
            sys.stderr.write(f"[us_movers] quote 실패: {e}\n")
    if not quotes:
        return None
    rows = []
    for q in quotes:
        sym = q.get("symbol") or ""
        if not sym:
            continue
        try:
            price = float(q.get("price") or 0)
            vol = float(q.get("volume") or 0)
        except Exception:
            continue
        try:
            mcap = float(q.get("marketCap") or 0)
        except Exception:
            mcap = 0.0
        # 페니주·소형주 배제: 주가 $5 미만 또는 시총 100억달러 미만 제외(잡주 필터)
        if price < 5.0:
            continue
        if mcap and mcap < 1.0e10:
            continue
        val = price * vol  # 거래대금(달러)
        if val <= 0:
            continue
        chg, _ = _pct_disp(q.get("changesPercentage", q.get("changePercentage")))
        rows.append({"symbol": sym, "name": q.get("name", ""),
                     "price": f"${price:,.2f}", "chg": chg, "_val": val})
    if not rows:
        return None
    rows.sort(key=lambda r: r["_val"], reverse=True)
    return [{kk: vv for kk, vv in r.items() if kk != "_val"} for r in rows[:n]]



def fetch_most_mentioned_us(fmp_key, lookback=120):
    """최근 미국 주식 뉴스에서 가장 많이 언급된 대형주 1개 — 실패 시 None.
    뉴스 목록의 종목 심볼 빈도를 세고, 상위 후보 중 대형주(주가 $5+·시총 100억$+)를 고른다."""
    if not fmp_key:
        return None
    k = urllib.parse.quote(fmp_key)
    arts = None
    for u in (f"https://financialmodelingprep.com/stable/news/stock-latest?limit={lookback}&apikey={k}",
              f"https://financialmodelingprep.com/api/v3/stock_news?limit={lookback}&apikey={k}"):
        try:
            arts = _fmp_get(u)
            if isinstance(arts, dict):
                arts = arts.get("data") or arts.get("content") or []
            if arts:
                break
        except Exception as e:
            sys.stderr.write(f"[most_mentioned] 뉴스 조회 실패: {e}\n")
    if not arts:
        return None
    from collections import Counter
    cnt = Counter()
    for a in arts:
        sym = str(a.get("symbol") or a.get("ticker") or "").strip().upper()
        if sym and sym.isalpha() and 1 <= len(sym) <= 5:
            cnt[sym] += 1
    if not cnt:
        return None
    for sym, _n in cnt.most_common(8):
        try:
            q = (_fmp_get(f"https://financialmodelingprep.com/stable/quote?symbol={sym}&apikey={k}")
                 or _fmp_get(f"https://financialmodelingprep.com/api/v3/quote/{sym}?apikey={k}"))
            if isinstance(q, list) and q:
                q = q[0]
            if not isinstance(q, dict):
                continue
            price = float(q.get("price") or 0); mcap = float(q.get("marketCap") or 0)
            if price < 5.0 or (mcap and mcap < 1.0e10):
                continue
            chg, _ = _pct_disp(q.get("changesPercentage", q.get("changePercentage")))
            return {"symbol": sym, "name": q.get("name", ""),
                    "price": f"${price:,.2f}", "chg": chg, "news": True, "mentions": _n}
        except Exception:
            continue
    return None


def combine_us_focus(base, news_pick, n=5):
    """미국 주목 n종목 = 거래대금 상위 4 + 뉴스 최다 언급 1.
    뉴스픽이 거래대금 상위4와 겹치거나 없으면 거래대금 5순위로 채운다."""
    if not base:
        return base
    top = base[:4]
    syms = {b.get("symbol") for b in top}
    if news_pick and news_pick.get("symbol") and news_pick["symbol"] not in syms:
        np = dict(news_pick); np["news"] = True
        return top + [np]
    return base[:n]  # 겹치거나 뉴스픽 없음 → 거래대금 상위 n


def _prem_disp(p):
    """김치 프리미엄(%) → ('+1.2%'|'-0.8%'|'0.0%', 'up'|'down'|'flat'). 소수 1자리."""
    try:
        v = round(float(p), 1)
    except Exception:
        return (None, "flat")
    if v > 0:
        return (f"+{v:.1f}%", "up")
    if v < 0:
        return (f"{v:.1f}%", "down")
    return ("0.0%", "flat")  # 반올림 후 0이면 보합(±0.0% 어색함 방지)


def _krw_big(won):
    """원화 큰 금액 → '1.2조원' / '3,450억원' / '—'(국내 거래대금 표기용)."""
    try:
        v = float(won)
    except Exception:
        return None
    if v >= 1e12:
        return f"{v/1e12:.1f}조원"
    if v >= 1e8:
        return f"{v/1e8:,.0f}억원"
    return f"{v:,.0f}원"


def fetch_crypto_movers(td_key=None):
    """업비트(원화)·바이낸스(USDT)·USD/KRW로 주요 코인 시세 + 김치 프리미엄 계산.
    공개 시장 데이터(인증/키 불필요)만 사용. 업비트=api.upbit.com,
    바이낸스=data-api.binance.vision(지역차단 회피). 실패 시 None(섹션 자동 생략)."""
    SPECS = [
        ("USDT", "테더",    "KRW-USDT", None),       # 스테이블코인: 기준환율 대비 프리미엄
        ("BTC",  "비트코인", "KRW-BTC",  "BTCUSDT"),
        ("ETH",  "이더리움", "KRW-ETH",  "ETHUSDT"),
        ("XRP",  "리플",    "KRW-XRP",  "XRPUSDT"),
    ]
    # 1) USD/KRW 기준환율 (Twelve Data — 이미 보유한 키 재사용, 별도 Secret 불필요)
    usdkrw = None
    if td_key:
        try:
            k = urllib.parse.quote(td_key)
            j = _fmp_get(f"https://api.twelvedata.com/price?symbol=USD/KRW&apikey={k}")
            if isinstance(j, dict) and j.get("price"):
                usdkrw = float(j["price"])
        except Exception as e:
            sys.stderr.write(f"[crypto] USD/KRW 환율 실패: {e}\n")
    # 2) 업비트 원화 시세 (공개 quotation, 인증 불필요)
    up = {}
    try:
        mk = ",".join(s[2] for s in SPECS)
        arr = _fmp_get(f"https://api.upbit.com/v1/ticker?markets={urllib.parse.quote(mk)}")
        for o in (arr or []):
            up[o.get("market")] = o
    except Exception as e:
        sys.stderr.write(f"[crypto] 업비트 시세 실패: {e}\n")
        return None
    if not up:
        return None
    # 3) 바이낸스 USDT 시세 (data-api.binance.vision — 공개·미국IP 차단 회피)
    bn = {}
    try:
        syms = [x[3] for x in SPECS if x[3]]
        q = urllib.parse.quote(json.dumps(syms, separators=(",", ":")))
        arr = _fmp_get(f"https://data-api.binance.vision/api/v3/ticker/price?symbols={q}")
        for o in (arr or []):
            try:
                bn[o.get("symbol")] = float(o.get("price"))
            except Exception:
                pass
    except Exception as e:
        sys.stderr.write(f"[crypto] 바이낸스 시세 실패: {e}\n")
    rows, headline = [], {}
    for sym, kname, umk, bsym in SPECS:
        uo = up.get(umk)
        if not uo:
            continue
        krw = uo.get("trade_price")
        try:
            chg_disp, chg_dir = _pct_disp(float(uo.get("signed_change_rate", 0)) * 100.0)
        except Exception:
            chg_disp, chg_dir = (None, "flat")
        prem_pct = prem_dir = None
        binance_disp = "—"
        if sym == "USDT":
            binance_disp = "$1.00"
            if usdkrw and krw:
                pp = (float(krw) / usdkrw - 1.0) * 100.0
                prem_pct, prem_dir = _prem_disp(pp); headline["USDT"] = pp
        else:
            bp = bn.get(bsym)
            if bp:
                binance_disp = (f"${bp:,.2f}" if bp >= 1 else f"${bp:,.4f}")
                if usdkrw and krw:
                    pp = (float(krw) / (bp * usdkrw) - 1.0) * 100.0
                    prem_pct, prem_dir = _prem_disp(pp)
                    if sym == "BTC":
                        headline["BTC"] = pp
        val24h = _krw_big(uo.get("acc_trade_price_24h"))   # 업비트 24h 거래대금(국내 수급 신호)
        rows.append({
            "sym": sym, "name": kname,
            "krw": (f"₩{float(krw):,.0f}" if krw else "—"),
            "chg": (chg_disp or "—"), "dir": chg_dir,
            "binance": binance_disp,
            "prem": prem_pct, "prem_dir": prem_dir,
            "val24h": val24h,
        })
    if not rows:
        return None
    return {"usdkrw": usdkrw, "rows": rows, "headline": headline}


def inject_stock_tables(body, token, key, sec):
    """본문의 {{STOCK|시장|식별자|종목명}} 토큰을 공식 4줄 표로 치환.
    미국 종목은 콤마로 묶어 1회만 호출(분당 제한 회피), 한국 종목은 KIS로 개별 조회."""
    import re
    body = body or ""
    fmp_key = os.environ.get("FMP_API_KEY", "").strip()
    pat = r"(?:<p>\s*)?\{\{\s*STOCK\s*\|\s*([a-zA-Z]+)\s*\|\s*([^|}]+?)\s*\|\s*([^}]*?)\s*\}\}(?:\s*</p>)?"
    toks = re.findall(pat, body)
    us_syms = [ident for (mkt, ident, _nm) in toks if mkt.strip().lower() == "us"]
    us_data = fmp_quotes(us_syms, fmp_key) if us_syms else {}

    # FMP에서 누락됐거나 값이 빈 미국 종목 → Twelve Data 2차 폴백
    # (가격·전일종가·등락률·거래량 보강. 시가총액은 무료 미제공이라 그 칸만 '—'.)
    def _is_blank(dd):
        return (not dd) or (dd.get("c") in (None, "—") and dd.get("pc") in (None, "—"))
    if us_syms:
        td_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
        need = sorted({s.upper() for s in us_syms if _is_blank(us_data.get(s.upper()))})
        if need and td_key:
            print(f"[td] FMP 누락 {len(need)}종목 → Twelve Data 폴백: {', '.join(need)}")
            for i, sym in enumerate(need):
                if i:
                    time.sleep(1)  # 무료 8req/분 → 호출 간격
                td = twelvedata_quote(sym, td_key)
                if td and not _is_blank(td):
                    us_data[sym] = td
                    print(f"[td] {sym} 보강 성공(시가총액 제외)")
        elif need and not td_key:
            sys.stderr.write("[td] TWELVEDATA_API_KEY 없음 — 폴백 불가(워크플로 env 확인)\n")
        # Twelve Data 이후에도 빈 종목 → 야후 차트 3차 폴백
        rem = sorted({s.upper() for s in us_syms if _is_blank(us_data.get(s.upper()))})
        if rem:
            print(f"[yh] 잔여 {len(rem)}종목 → 야후 차트 3차 폴백: {', '.join(rem)}")
            for sym in rem:
                yq = yahoo_stock_fallback(sym)
                if yq and not _is_blank(yq):
                    us_data[sym] = yq
                    print(f"[yh] {sym} 보강 성공(시가총액 제외)")
        # 세 소스 모두 실패해 빈 표가 될 종목 경고(아침에 바로 눈에 띄게)
        still = sorted({s.upper() for s in us_syms if _is_blank(us_data.get(s.upper()))})
        if still:
            sys.stderr.write(f"[stock][경고] FMP·TwelveData 모두 실패 → 빈 표: {', '.join(still)}\n")

    kr_cache = {}
    def repl(m):
        market = (m.group(1) or "").strip().lower()
        ident  = (m.group(2) or "").strip()
        name   = (m.group(3) or "").strip()
        if market == "us":
            d = us_data.get(ident.upper())
        elif market == "kr":
            if ident not in kr_cache:
                kr_cache[ident] = kis_stock(ident, token, key, sec) or yahoo_kr_stock_fallback(ident)
            d = kr_cache[ident]
        else:
            d = None
        return std_stock_table(name, ident, d)
    return re.sub(pat, repl, body)


def inject_crypto_table(body, crypto):
    """본문의 {{CRYPTO_TABLE}} 토큰을 업비트·바이낸스·김프 5열 표로 치환.
    crypto가 없으면 토큰만 제거(섹션 자동 생략). 공개 시세라 키 불필요."""
    import re as _re
    body = body or ""
    pat = r"(?:<p>\s*)?\{\{\s*CRYPTO_TABLE\s*\}\}(?:\s*</p>)?"
    if not crypto or not crypto.get("rows"):
        return _re.sub(pat, "", body)
    rows_html = []
    for r in crypto["rows"]:
        ch = r.get("chg")
        if ch and ch != "—":
            cc = _CHGCOLOR.get(r.get("dir", "flat"), "#1a1a1a")
            chg_cell = f'<span style="color:{cc};font-weight:600">{esc(ch)}</span>'
        else:
            chg_cell = "—"
        pr = r.get("prem")
        if pr:
            pc = _CHGCOLOR.get(r.get("prem_dir", "flat"), "#1a1a1a")
            prem_cell = f'<span style="color:{pc};font-weight:700">{esc(pr)}</span>'
        else:
            prem_cell = "—"
        rows_html.append(
            "<tr>"
            f'<td><strong>{esc(r.get("sym",""))}</strong> {esc(r.get("name",""))}</td>'
            f'<td>{esc(r.get("krw","—"))}</td>'
            f'<td>{chg_cell}</td>'
            f'<td>{esc(r.get("binance","—"))}</td>'
            f'<td>{prem_cell}</td>'
            "</tr>")
    fx = crypto.get("usdkrw")
    fxnote = (f"기준환율 USD/KRW ₩{float(fx):,.2f} · " if fx else "")
    table = (
        '<div class="src-cat">암호화폐 시세 · 김치 프리미엄</div>'
        '<table class="grid"><thead><tr>'
        '<th>코인</th><th>업비트(₩)</th><th>24h</th><th>바이낸스($)</th><th>김프</th>'
        '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table>'
        f'<p class="small">{fxnote}김프(김치 프리미엄)=업비트 원화가가 '
        '\u2018바이낸스 USDT가\u00d7기준환율\u2019 대비 얼마나 비싼지(테더는 기준환율 대비). '
        '(+)=국내 프리미엄(빨강)\u00b7(\u2212)=역프리미엄(파랑). '
        '출처: 업비트\u00b7바이낸스\u00b7Twelve Data, 작성시점 공개 시세.</p>')
    return _re.sub(pat, table, body)



# ----------------------------- 자동 막대 차트(인라인 SVG) -----------------------------
# 브라우저 폰트로 한글 렌더 → matplotlib/폰트파일 의존 없음. 텍스트는 코드가 직접 써서 오타 통제.
# 관례: 지수 일간 등락률=세로 막대, 종목/코인 순위=가로 막대(KRX 등락률 상하위 방식).
_CH_FONT = "font-family:'Noto Sans KR','Malgun Gothic','Apple SD Gothic Neo',sans-serif"
def _ch_esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def _ch_col(d):
    return {"up": "#C0392B", "down": "#1B5E9B"}.get(d, "#6b6b6b")

def _parse_pct(s):
    if s is None:
        return None
    t = str(s).replace("%", "").replace("\u2212", "-").replace("+", "").replace(",", "").strip()
    if not t or t in ("\u2014", "-"):
        return None
    try:
        v = float(t)
    except Exception:
        return None
    return (v, "up" if v > 0 else ("down" if v < 0 else "flat"))

def svg_bar_v(title, sub, data, note=""):
    """세로 막대(지수 등락률 비교). data=[(label,pct,dir)]"""
    W, H = 660, 368; padL, padR, padT, padB = 46, 24, 80, 60
    plotW, plotH = W - padL - padR, H - padT - padB
    vals = [d[1] for d in data]
    vmax = max(vals + [0.0]); vmin = min(vals + [0.0]); span = (vmax - vmin) or 1.0
    vmax += span * 0.20; vmin -= (span * 0.20 if vmin < 0 else 0.0); span = (vmax - vmin) or 1.0
    def y(v): return padT + plotH * (vmax - v) / span
    zeroY = y(0.0); n = len(data); slot = plotW / n; bw = min(slot * 0.46, 58)
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
         f'style="width:100%;height:auto;max-width:660px;display:block;margin:0 auto;{_CH_FONT}">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    p.append(f'<text x="{padL-8}" y="30" font-size="19" font-weight="700" fill="#16335C">{_ch_esc(title)}</text>')
    p.append(f'<rect x="{padL-8}" y="39" width="160" height="3" fill="#C9A654"/>')
    if sub: p.append(f'<text x="{padL-8}" y="58" font-size="12" fill="#5b5b5b">{_ch_esc(sub)}</text>')
    p.append(f'<line x1="{padL}" y1="{zeroY:.1f}" x2="{W-padR}" y2="{zeroY:.1f}" stroke="#cccccc" stroke-width="1"/>')
    for i, (lab, v, d) in enumerate(data):
        cx = padL + slot * i + slot / 2; c = _ch_col(d); yv = y(v)
        top = min(yv, zeroY); h = max(abs(yv - zeroY), 1)
        p.append(f'<rect x="{cx-bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="2.5" fill="{c}"/>')
        sign = "+" if v > 0 else ("\u2212" if v < 0 else "")
        vy = (top - 7) if v >= 0 else (yv + 17)
        p.append(f'<text x="{cx:.1f}" y="{vy:.1f}" font-size="13" font-weight="700" fill="{c}" text-anchor="middle">{sign}{abs(v):.2f}%</text>')
        p.append(f'<text x="{cx:.1f}" y="{H-padB+24:.1f}" font-size="12.5" fill="#333333" text-anchor="middle">{_ch_esc(lab)}</text>')
    p.append(f'<text x="{padL-8}" y="{H-12}" font-size="10" font-weight="700" fill="#C9A654">INVEST STORY</text>')
    if note: p.append(f'<text x="{W-padR}" y="{H-12}" font-size="9.5" fill="#9a9a9a" text-anchor="end">{_ch_esc(note)}</text>')
    p.append('</svg>'); return "".join(p)

def svg_bar_h(title, sub, data, note="", decimals=1):
    """가로 막대(종목/코인 등락·김프 순위). data=[(label,value,dir)]"""
    W = 660; rowH = 36; padT = 82; padB = 46; padL = 176; padR = 76
    H = padT + rowH * len(data) + padB; barMaxW = W - padL - padR
    vmax = max([abs(d[1]) for d in data] + [0.001])
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
         f'style="width:100%;height:auto;max-width:660px;display:block;margin:0 auto;{_CH_FONT}">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    p.append(f'<text x="{padL-24}" y="32" font-size="19" font-weight="700" fill="#16335C">{_ch_esc(title)}</text>')
    p.append(f'<rect x="{padL-24}" y="41" width="160" height="3" fill="#C9A654"/>')
    if sub: p.append(f'<text x="{padL-24}" y="60" font-size="12" fill="#5b5b5b">{_ch_esc(sub)}</text>')
    for i, (lab, v, d) in enumerate(data):
        cy = padT + rowH * i; c = _ch_col(d); bw = barMaxW * abs(v) / vmax
        p.append(f'<text x="{padL-12}" y="{cy+rowH/2+4:.1f}" font-size="12.5" fill="#333333" text-anchor="end">{_ch_esc(lab)}</text>')
        p.append(f'<rect x="{padL}" y="{cy+7:.1f}" width="{max(bw,1):.1f}" height="{rowH-16}" rx="2.5" fill="{c}"/>')
        sign = "+" if v > 0 else ("\u2212" if v < 0 else "")
        p.append(f'<text x="{padL+bw+9:.1f}" y="{cy+rowH/2+4:.1f}" font-size="12.5" font-weight="700" fill="{c}">{sign}{abs(v):.{decimals}f}%</text>')
    p.append(f'<text x="{padL-24}" y="{H-12}" font-size="10" font-weight="700" fill="#C9A654">INVEST STORY</text>')
    if note: p.append(f'<text x="{W-padR}" y="{H-12}" font-size="9.5" fill="#9a9a9a" text-anchor="end">{_ch_esc(note)}</text>')
    p.append('</svg>'); return "".join(p)



# ----------------------------- 김치 프리미엄 히스토리(90일 누적) -----------------------------
_KIMCHI_COLORS = [("USDT", "#6B7785"), ("BTC", "#C9A654"), ("ETH", "#16335C"), ("XRP", "#7fb0e8")]

def _load_kimchi_history():
    """data/kimchi_history.json 로드. 없으면 []."""
    try:
        with open(KIMCHI_HISTORY, encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, list) else []
    except Exception:
        return []

def update_kimchi_history(crypto, mode):
    """이번 발행분 김프를 히스토리에 추가(같은 날짜·모드는 교체), 90일 초과분 앞쪽 절삭 후 저장."""
    try:
        if not crypto or not crypto.get("rows"):
            return _load_kimchi_history()
        prem = {}
        for r in crypto["rows"]:
            s = str(r.get("sym", "")).strip()
            m = re.search(r"([+\-\u2212]?\d+(?:\.\d+)?)\s*%", str(r.get("prem", "")))
            if s and m:
                prem[s] = float(m.group(1).replace("\u2212", "-"))
        if not prem:
            return _load_kimchi_history()
        today = datetime.now(KST).strftime("%Y-%m-%d")
        hist = [e for e in _load_kimchi_history()
                if not (e.get("date") == today and e.get("mode") == mode)]
        hist.append({"date": today, "mode": mode, "prem": prem})
        hist.sort(key=lambda e: (e.get("date", ""), 0 if e.get("mode") == "open" else 1))
        cutoff = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")
        hist = [e for e in hist if e.get("date", "") >= cutoff]
        os.makedirs(os.path.dirname(KIMCHI_HISTORY), exist_ok=True)
        with open(KIMCHI_HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        print(f"[daily_news] 김프 히스토리 갱신: {len(hist)}건 (90일 롤링)")
        return hist
    except Exception as e:
        sys.stderr.write(f"[kimchi] 히스토리 갱신 경고(무시): {e}\n")
        return _load_kimchi_history()

def svg_line_kimchi(hist):
    """코인별 김치 프리미엄 추이 선 그래프(최근 90일). 데이터 2건 미만이면 None."""
    hist = [e for e in (hist or []) if e.get("prem")]
    if len(hist) < 2:
        return None
    import datetime as _d
    def _x_of(e):
        d = _d.datetime.strptime(e["date"], "%Y-%m-%d")
        return d.toordinal() + (0.38 if e.get("mode") == "open" else 0.65)
    xs = [_x_of(e) for e in hist]
    x0, x1 = min(xs), max(xs)
    if x1 - x0 < 1e-9:
        x1 = x0 + 1.0
    vals = [v for e in hist for v in e["prem"].values()]
    vmax = max(vals + [0.0]) + 0.4
    vmin = min(vals + [0.0]) - 0.4
    span = (vmax - vmin) or 1.0
    W, H = 660, 350
    padL, padR, padT, padB = 56, 24, 96, 46
    plotW, plotH = W - padL - padR, H - padT - padB
    def X(x): return padL + plotW * (x - x0) / (x1 - x0)
    def Y(v): return padT + plotH * (vmax - v) / span
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
         f'style="width:100%;height:auto;max-width:{W}px;display:block;margin:0 auto;{_CH_FONT}">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{padL-32}" y="30" font-size="19" font-weight="700" fill="#16335C">암호화폐 김치 프리미엄 추이</text>',
         f'<rect x="{padL-32}" y="39" width="160" height="3" fill="#C9A654"/>',
         f'<text x="{padL-32}" y="58" font-size="12" fill="#5b5b5b">자체 집계 · (\u2212)=역프리미엄 · 최근 90일 롤링</text>']
    # 범례(최신값 포함)
    last = hist[-1]["prem"]
    lx = padL - 32
    for sym, col in _KIMCHI_COLORS:
        if not any(sym in e["prem"] for e in hist):
            continue
        lv = last.get(sym)
        lab = f"{sym} {('+' if (lv or 0) > 0 else '')}{lv:.1f}%" if lv is not None else sym
        p.append(f'<rect x="{lx}" y="70" width="10" height="10" rx="2" fill="{col}"/>')
        p.append(f'<text x="{lx+15}" y="79" font-size="11.5" font-weight="700" fill="#333333">{lab}</text>')
        lx += 15 + 9 * len(lab) + 18
    # y 격자·라벨
    step = 1.0 if span > 2.4 else 0.5
    v = (int(vmin / step)) * step
    while v <= vmax:
        yy = Y(v)
        if padT - 4 <= yy <= H - padB + 4:
            if abs(v) > 1e-9:
                p.append(f'<line x1="{padL}" y1="{yy:.1f}" x2="{W-padR}" y2="{yy:.1f}" stroke="#ececec"/>')
            else:
                p.append(f'<line x1="{padL}" y1="{yy:.1f}" x2="{W-padR}" y2="{yy:.1f}" stroke="#999999" stroke-dasharray="5 4"/>')
            p.append(f'<text x="{padL-8}" y="{yy+4:.1f}" font-size="10.5" fill="#8a8a8a" text-anchor="end">{("+" if v>0 else "")}{v:.1f}%</text>')
        v += step
    # x 라벨(최대 6개 날짜)
    seen = []
    for e in hist:
        if e["date"] not in seen:
            seen.append(e["date"])
    stepn = max(1, (len(seen) + 5) // 6)
    for d in seen[::stepn]:
        xx = X(_d.datetime.strptime(d, "%Y-%m-%d").toordinal() + 0.5)
        if padL <= xx <= W - padR:
            p.append(f'<text x="{xx:.1f}" y="{H-padB+22:.1f}" font-size="10.5" fill="#666666" text-anchor="middle">{d[5:].replace("-","/")}</text>')
    # 코인별 선
    for sym, col in _KIMCHI_COLORS:
        pts = [(X(_x_of(e)), Y(e["prem"][sym])) for e in hist if sym in e["prem"]]
        if len(pts) < 2:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        p.append(f'<polyline points="{path}" fill="none" stroke="{col}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>')
        p.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3.2" fill="{col}"/>')
    p.append(f'<text x="{padL-32}" y="{H-10}" font-size="10" font-weight="700" fill="#C9A654">INVEST STORY</text>')
    p.append(f'<text x="{W-padR}" y="{H-10}" font-size="9.5" fill="#9a9a9a" text-anchor="end">출처: 업비트·바이낸스 자체 집계</text>')
    p.append('</svg>')
    return "".join(p)


def _chart_fig(svg, cap):
    return ('<figure style="margin:24px 0">' + svg +
            '<figcaption style="margin-top:8px;font-size:12.5px;color:#6b6b6b;'
            'text-align:center;line-height:1.5">' + _ch_esc(cap) + '</figcaption></figure>')

def inject_charts(body, mode, items, rank_up=None, rank_dn=None, vol=None, us_movers=None, crypto=None):
    """본문에 자동 막대 차트(주요 지수 등락률 + 종목/코인)를 삽입. 실패 시 원본 유지(비치명)."""
    try:
        top_figs, end_figs = [], []
        # 차트1: 주요 지수 등락률(세로)
        d1 = []
        for nm in ("KOSPI", "KOSDAQ", "S&P 500", "나스닥", "다우존스", "러셀 2000"):
            it = (items or {}).get(nm)
            if not it:
                continue
            pr = _parse_pct(it.get("change"))
            if pr:
                d1.append((nm, pr[0], pr[1]))
        if len(d1) >= 2:
            top_figs.append(_chart_fig(
                svg_bar_v("주요 지수 등락률", ("개장" if mode == "open" else "마감") + " 브리핑 · 전일 대비", d1, "출처: 지수 데이터"),
                "그림. 주요 지수 일간 등락률(막대) — 상승=빨강/하락=파랑."))
        # 차트2: 종목/코인(가로) — close: 등락상위 / open: 미국주목 / 폴백: 코인 김프
        d2 = []; t2 = ""; s2 = ""; dec = 1
        if mode == "close" and rank_up:
            for r in rank_up[:5]:
                pr = _parse_pct(r.get("ctrt")); nm = r.get("name", "")
                if nm and pr:
                    d2.append((nm[:12], pr[0], pr[1]))
            t2, s2, dec = "등락률 상위 종목", "당일 상승률 상위(시총 100위 이내)", 2
        if not d2 and us_movers:
            for r in us_movers[:5]:
                pr = _parse_pct(r.get("chg"))
                nm = (str(r.get("symbol", "")) + " " + str(r.get("name", ""))).strip()
                if nm and pr:
                    d2.append((nm[:16], pr[0], pr[1]))
            t2, s2, dec = "간밤 미국 주목 종목", "거래대금 상위 · 등락률", 2
        # (변경 2026-07-03) 김프는 일간 막대 대신 아래 '추이 선 그래프'로 상시 표시
        if len(d2) >= 2:
            end_figs.append(_chart_fig(svg_bar_h(t2, s2, d2, "출처: 거래소 데이터", decimals=dec), f"그림. {t2}(막대)."))
        if crypto and crypto.get("rows"):
            _ksvg = svg_line_kimchi(_load_kimchi_history())
            if _ksvg:
                end_figs.append(_chart_fig(_ksvg, "그림. 암호화폐 김치 프리미엄 추이(선) — 코인별 색상, 최근 90일 자체 집계."))
        if not top_figs and not end_figs:
            return body
        out = body
        if top_figs:
            m = re.search(r'(<p class="lead-para">.*?</p>)', out, re.S)
            if m:
                out = out[:m.end()] + "\n" + "".join(top_figs) + out[m.end():]
            else:
                out = "".join(top_figs) + out
        if end_figs:
            out = out + "\n" + "".join(end_figs)
        return out
    except Exception as e:
        sys.stderr.write(f"[charts] 자동 차트 삽입 경고(무시): {e}\n")
        return body



# ----------------------------- 섹션 요약 카드(인라인 SVG) -----------------------------
def _wrap_ko(text, maxchars):
    out, cur = [], ""
    for w in str(text).split(" "):
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= maxchars:
            cur += " " + w
        else:
            out.append(cur); cur = w
        while len(cur) > maxchars:
            out.append(cur[:maxchars]); cur = cur[maxchars:]
    if cur:
        out.append(cur)
    return out[:4]

def svg_summary_card(eyebrow, text, tone="neutral"):
    """섹션 핵심을 한눈에 보여주는 요약 카드(SVG). 글자는 브라우저 폰트로 렌더."""
    stripe = {"up": "#C0392B", "down": "#1B5E9B"}.get(tone, "#C9A654")
    W = 660; padX = 28; lineH = 24; maxchars = 33
    lines = _wrap_ko(text, maxchars) or [""]
    body_top = 50
    H = body_top + len(lines) * lineH + 18
    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
         f'aria-label="{_ch_esc(eyebrow)} 핵심 요약" '
         f'style="width:100%;height:auto;max-width:660px;display:block;margin:0 auto;{_CH_FONT}">']
    p.append(f'<rect x="1.5" y="1.5" width="{W-3}" height="{H-3}" rx="12" fill="#faf9f7" stroke="#ece7dc"/>')
    p.append(f'<rect x="1.5" y="1.5" width="6" height="{H-3}" rx="3" fill="{stripe}"/>')
    p.append(f'<text x="{padX}" y="31" font-size="12.5" font-weight="700" fill="{stripe}">{_ch_esc(eyebrow)} · 핵심 요약</text>')
    for i, ln in enumerate(lines):
        p.append(f'<text x="{padX}" y="{body_top+16+i*lineH}" font-size="15" fill="#1f2a44">{_ch_esc(ln)}</text>')
    p.append('</svg>')
    return "".join(p)

def inject_section_cards(body):
    """각 섹션의 마무리 문장(.takeaway)을 섹션별 '요약 카드' 이미지로 변환(섹션당 1장). 비치명."""
    try:
        def repl(m):
            text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if not text:
                return m.group(0)
            pre = body[:m.start()]
            hm = re.findall(r'<h2[^>]*>(.*?)</h2>', pre, re.S)
            eyebrow = re.sub(r'<[^>]+>', '', hm[-1]).strip()[:22] if hm else "이 섹션"
            card = svg_summary_card(eyebrow, text)
            return f'<figure style="margin:18px 0">{card}</figure>'
        return re.sub(r'<p class="takeaway">(.*?)</p>', repl, body, flags=re.S)
    except Exception as e:
        sys.stderr.write(f"[cards] 섹션 요약 카드 경고(무시): {e}\n")
        return body


# ----------------------------- Claude API 작성 -----------------------------
ANTHROPIC_MODEL = "claude-sonnet-4-6"

AI_CLASSES = (
    "사용 가능한 HTML 클래스(이 클래스들만 사용):\n"
    " - <p class=\"lead-para\">…</p> : 첫 리드 문단\n"
    " - <section class=\"sec\"><div class=\"eyebrow\">SECTION</div><h2>제목</h2></section> : 섹션 헤더(섹션마다 본문 <p>가 뒤따름)\n"
    " - <div class=\"src-cat\">소제목</div> : 표/블록 위 작은 소제목\n"
    " - <table class=\"grid\"><thead><tr><th>…</th></tr></thead><tbody><tr><td>…</td></tr></tbody></table> : 데이터 표\n"
    " - 등락은 <span class=\"up\">+1.2%</span> / <span class=\"down\">-0.8%</span> (한국식: 상승=빨강 up, 하락=파랑 down)\n"
    " - <span class=\"key\">핵심 구절</span> : 꼭 읽혀야 할 핵심 키워드·사건명·핵심 수치 구절에 골드 밑줄 강조. 한 문단에 1~3개까지 적극적으로(단, 한 문단을 통째로 칠하는 남발은 금지).\n"
    " - <span class=\"num\">8,203.84</span> : 본문 속 중요한 '절대 수치'(지수 레벨·금액·종목수 등)를 세리프로 환기. 등락률(%)에는 쓰지 말 것 — 그건 up/down.\n"
    " - <p class=\"takeaway\">섹션 핵심 한 줄</p> : 섹션마다 가장 중요한 결론 1문장을 콜아웃 박스로(섹션당 최대 1개, 보통 섹션 끝에).\n"
    " - <strong>…</strong> : 부차(약) 강조. key/num에 해당 안 되는 일반 강조용.\n"
    " - <p class=\"small\">…</p> : 작은 보조설명·출처\n"
    " - <hr class=\"rule\"> : 구분선\n"
    " - 출처 링크: <a href=\"URL\" target=\"_blank\" rel=\"noopener\">매체명</a>\n"
    "\n[강조 규칙 — 색=역할 1:1 고정 · 강조는 '필수']\n"
    " · 본문에 강조가 하나도 없으면 안 됩니다. 평문만 늘어놓지 말고 아래 4종을 기사 전체에 골고루 반드시 적용하세요.\n"
    " · 빨강(up)·파랑(down)은 오직 '일간 등락률 수치'에만 — 본문에 등장하는 모든 등락률(%)은 빠짐없이 .up/.down으로 감쌀 것(필수).\n"
    " · .key(골드밑줄): 각 섹션 본문에서 핵심 사건명·키워드·중요 구절에 밑줄을 적극적으로 표시(문단당 2~3개까지 권장, 한 문단 통째 남발만 금지).\n"
    " · .num(세리프): 지수 레벨·중요 금액·종목수 같은 핵심 '절대수치'가 본문에 나오면 반드시 .num으로 환기(등락률(%)엔 쓰지 말 것 — 그건 up/down).\n"
    " · .takeaway: 각 섹션은 가장 중요한 결론 1문장을 .takeaway 콜아웃으로 마무리(섹션당 1개 권장, 최대 1개).\n"
    " · 원칙은 '평문이 기본, 강조는 포인트'지만 포인트가 0개여서는 안 됩니다 — 색이 흔해지지 않게 절제하되, 각 종류가 최소 1회 이상은 쓰여야 합니다.\n"
)


def _ai_user_prompt(mode, date_kst, items, asof, vol, rank_up, rank_dn, flows, us_movers=None, crypto=None, correction_note=None):
    d = date_kst
    dstr = f'{d.year}년 {d.month}월 {d.day}일({WEEKDAY_KR[d.weekday()]})'
    lines = [f'[확정 데이터 — 아래 숫자는 그대로 사용, 추가 사실은 web_search로 확인]',
             f'작성시각(KST): {asof or dstr}', f'발행일: {dstr}', f'모드: {mode}']
    idx = []
    for nm in ["KOSPI", "KOSDAQ", "USD/KRW", "WTI", "S&P 500", "나스닥", "다우존스", "달러인덱스",
               "미국 10년물 금리", "미국 30년물 금리", "KRX 금", "국제 금"]:
        it = items.get(nm)
        if it:
            idx.append(f'{nm} {it.get("value","")}({it.get("change","")})')
    if idx:
        lines.append("지수/지표: " + ", ".join(idx))
    if items.get("KRX 금") or items.get("국제 금"):
        lines.append("(금 표기 규칙 — 'KRX 금'은 한국거래소 금시장 '금 99.99_1Kg' 기준 원/그램(원/g), "
                     "'국제 금'은 런던 현물 XAU/USD 기준 달러/트로이온스입니다. 본문에 쓸 때 단위를 반드시 "
                     "함께 표기하고, 단위·통화가 다르므로 두 값을 직접 비교하지 마세요. 국제 금은 선물이 "
                     "아니라 현물이므로 '금 선물'로 쓰지 마세요. 안전자산 선호·달러 흐름 맥락에서 한 문단 "
                     "이내로 다루면 충분합니다.)")
    if vol:
        lines.append("거래대금 상위(한국): " + "; ".join(
            f'{x["name"]}({x.get("code","")}) 현재가 {x["price"]} 등락 {x["ctrt"]}% 거래대금 {x.get("val","—")}' for x in vol))
    if rank_up:
        lines.append("등락률 상위: " + ", ".join(f'{x["name"]}({x.get("code","")}) {x["ctrt"]}%' for x in rank_up))
    if rank_dn:
        lines.append("등락률 하위: " + ", ".join(f'{x["name"]}({x.get("code","")}) {x["ctrt"]}%' for x in rank_dn))
    if rank_up or rank_dn:
        lines.append("(위 등락률 상·하위 종목은 '시가총액 상위 100위 이내'로 이미 필터링된 목록입니다. 등락률 상·하위 섹션에는 제공된 이 종목들만 다루고, 100위권 밖 종목을 임의로 추가하지 마세요.)")
    if flows:
        lines.append("수급(외국인/기관/개인 순매수): " + "; ".join(
            f'{m} 외 {v.get("frgn")} 기 {v.get("orgn")} 개 {v.get("indv")}' for m, v in flows.items()))
    if us_movers:
        lines.append("미국 주목 5종목(거래대금 상위 4 + 뉴스·화제성 픽 1): " + "; ".join(
            f'{x["symbol"]} {x.get("name","")} {x.get("price","")} {x.get("chg","")}'
            + (" (뉴스·화제성 픽)" if x.get("news") else "") for x in us_movers)
            + " — '뉴스·화제성 픽' 종목은 심층 분석에서 그 점을 명시하세요.")
    if crypto and crypto.get("rows"):
        segs = []
        for r in crypto["rows"]:
            seg = f'{r["sym"]} 업비트 {r.get("krw","—")} 24h {r.get("chg","—")}'
            if r.get("binance") and r["binance"] != "—":
                seg += f' 바이낸스 {r["binance"]}'
            if r.get("prem"):
                seg += f' 김프 {r["prem"]}'
            if r.get("val24h"):
                seg += f' 업비트24h거래대금 {r["val24h"]}'
            segs.append(seg)
        fx = crypto.get("usdkrw")
        lines.append("암호화폐(공개 시세·김치프리미엄, 아래 수치 그대로 사용): "
                     + ((f"기준환율 USD/KRW {fx:,.2f}; ") if fx else "")
                     + "; ".join(segs))

    if mode == "close":
        focus = (
            "이번은 '한국 증시 마감 시황'입니다. 한국 뉴스 위주로 구성하되, 밤사이/장중 한국 증시에 "
            "영향을 준 해외(특히 미국) 이슈가 있으면 반드시 포함하세요. 거래대금 상위 5종목(한국)은 각각 "
            "오늘 주가 흐름 + 관련 뉴스 + 수급(외국인·기관)을 엮어 분석하고, 근거를 댄 향후 주가 시나리오를 제시하세요.")
        sections = ("마감 요약(lead-para) → 지수 마감표(table.grid) → 오늘의 주요 뉴스(출처 명시) → "
                    "거래대금 상위 5종목 분석(종목별 흐름·뉴스·수급·향후 시나리오) → 외국인·기관 수급 분석 → "
                    "내일 관전 포인트·전망(근거 명시)")
    else:
        focus = (
            "이번은 '개장 브리핑'입니다. 간밤 미국 증시·주요 미국 뉴스 위주로 구성하되, 한국 장에 큰 영향을 "
            "줄 이슈가 있으면 포함하세요. 위 '미국 주목 종목'(제공된 간밤 거래활발 상위 미국 종목)을 중심으로 각 종목의 흐름·뉴스·향후 시나리오를 분석하되, 본문에 {{STOCK|us|티커|한글명}} 토큰을 넣어 표가 자동으로 채워지게 하고(최신 보조정보는 web_search로 보강), "
            "전망을 제시하세요. 한국 코스피·코스닥은 전 거래일 종가 기준으로 출발 환경을 짚어주세요. "
            "간밤 미국 지수표에는 S&P500·나스닥·다우존스를 모두 넣되, 종가(레벨) 칸을 비우거나 '—'로 두지 말고 "
            "확정 데이터에 없으면 web_search로 확인한 마감 지수를 채우세요.")
        sections = ("개장 요약(lead-para) → 간밤 미국 지수·지표표(table.grid) → 간밤 주요 뉴스(출처 명시) → "
                    "미국 주목 5종목 분석 → 오늘 한국 증시 관전 포인트·전망(근거 명시)")

    crypto_block = ""
    if crypto and crypto.get("rows"):
        sections = sections + " → 암호화폐 시황·수급·뉴스 분석"
        crypto_block = (
            "\n[암호화폐 섹션 — 필수: 당일 시황·수급·뉴스 분석]\n"
            " · 위 '암호화폐' 확정 수치를 토대로 당일 암호화폐 시장을 '수급'과 '뉴스' 중심으로 분석하는 섹션을 작성하세요(2~3문단). 단순 시세 나열이 아니라 '왜 이렇게 움직였는가'를 풀어야 합니다.\n"
            " · [수급] ① 비트코인·테더 김치 프리미엄의 부호·의미(+ = 국내 매수세 과열/역프 = 해외 대비 저평·차익 유인), ② 업비트 24h 거래대금으로 본 국내 거래 활발도, ③ web_search로 확인되는 글로벌 수급 — 미국 현물 비트코인·이더리움 ETF 순유입/유출, 기관·고래 동향, 스테이블코인 수급 흐름을 엮어 해석하세요.\n"
            " · [뉴스] 당일(또는 간밤) 암호화폐 시장을 움직인 주요 뉴스 — 규제·정책, 매크로(금리·달러), 거래소·온체인·주요 프로젝트 이슈 등을 web_search로 확인해 출처와 함께 1~3건 정리하세요(직접 인용 금지, 자신의 말로 요약).\n"
            " · 마지막에 향후 관전 포인트를 한 줄로(예정 이벤트는 KST 병기). 단정적 매수·매도 권유는 금지.\n"
            " · 표는 직접 쓰지 말고, 섹션 본문 안 독립된 한 줄로 {{CRYPTO_TABLE}} 토큰을 넣으면 코드가 업비트·바이낸스·김프 표를 자동으로 채웁니다(절대 <p> 안에 넣지 말 것).\n"
            " · 확정 수치(가격·김프·거래대금)는 위 제공값만 사용하고 임의 생성 금지. ETF 자금·뉴스 등 web_search로 확인한 사실은 반드시 출처를 답니다. 김프/등락 부호 색상은 상승=빨강(up)/하락=파랑(down) 원칙.\n"
        )
    body = "\n".join(lines)
    return (
        f"{body}\n\n"
        f"[작성 지침]\n{focus}\n\n"
        f"구성 순서: {sections}\n\n"
        f"{crypto_block}"
        "요구사항:\n"
        "1) 모든 수치·뉴스·주장에 출처를 명시하세요(web_search로 확인한 매체명+가능하면 링크). 확인 안 된 사실은 쓰지 마세요.\n"
        "2) 전망/주가 예상은 반드시 '근거 → 결론' 순서로, 시나리오(상승/하락/횡보 등)와 트리거를 함께. 단정적 매수·매도 권유는 금지.\n"
        "3) 뉴스는 직접 인용 대신 자신의 말로 요약(저작권). 출처만 표기.\n"
        "4) 분량은 충실하게(읽을거리 있는 데일리 뉴스레터 수준). 과장·미확인 추측 금지.\n"
        "5) 한국 증시 색상 관례: 상승=빨강(class=\"up\"), 하락=파랑(class=\"down\").\n"
        "6) 종목 심층 분석 — 데이터 표는 절대 직접 쓰지 마세요(숫자가 틀릴 수 있음). 분석할 종목마다 소제목 다음 줄에 "
        "아래 '종목 토큰'을 독립된 한 줄로(절대 <p> 안에 넣지 말 것) 넣으면, 코드가 그 자리에 "
        "전일 종가·금일 종가·거래량·시가총액 4줄 공식 표를 자동으로 채웁니다.\n"
        "   · 토큰 형식: {{STOCK|시장|식별자|종목명}}   (시장 = us 또는 kr)\n"
        "   · 미국: 식별자=티커(상장된 실제 종목만 — 비상장 기업은 토큰 쓰지 말 것)  예) {{STOCK|us|INTC|인텔}}\n"
        "   · 한국: 식별자=6자리 종목코드  예) {{STOCK|kr|005930|삼성전자}}  — 위 '확정 데이터'에 적힌 코드만 사용(모르면 토큰 없이 글로만 언급).\n"
        "   · '확인 중'이라고 절대 쓰지 마세요. 종가·거래량·시가총액 숫자를 본문에 직접 적지 말고 토큰에 맡기세요.\n\n"
        "7) 등락률 표기 통일: 전일 대비 '일간 등락률'에는 항상 +/− 부호를 붙입니다(예: <span class=\"up\">+1.2%</span>, <span class=\"down\">−9.99%</span>). 단, 기준금리·물가율·지수 레벨처럼 '변동이 아닌 값'에는 부호를 붙이지 마세요.\n"
        "8) 국가 병기 순서: 한국이 들어가는 국가 나열은 항상 한국을 맨 앞에 표기합니다(예: 한·일, 한·미, 한·중, 한·미·일). 일·한, 미·한 같은 표기는 금지합니다.\n"
        "9) 연휴·주말 후 첫 발행(월요일 개장 등): 섹션명은 '간밤'이 아니라 '주말간(또는 연휴간) 주요 뉴스'로 씁니다. 각 뉴스에 발생 날짜를 명시하고, 직전 호에서 이미 다룬 사건은 새 소식처럼 재보도하지 말고 '복습/요약'으로 구분해 표기합니다. 휴장으로 새 세션이 없으면 '간밤 마감'이라는 표현 자체를 금지합니다.\n"
        "10) 지수 종가·등락률(코스피·코스닥 등)은 반드시 본 프롬프트에 제공된 실데이터 값을 그대로 사용합니다. 웹 검색으로 찾은 지수 수치로 대체하는 것을 금지하며, 실데이터와 불일치하는 기사는 자동 검산에 걸려 발행이 차단됩니다.\n"
        "8) 쉬운 풀이: 중학생이 모를 만한 경제·금융 용어는 처음 나올 때 괄호로 짧게 뜻을 병기하세요(예: 서킷브레이커(주가가 급락하면 거래를 잠시 멈추는 제도), 밸류에이션(기업가치 대비 주가 수준)). 한 용어당 한 번만, 간결하게.\n"
        "9) 미국 지수·지표·기관 알파벳 병기: 처음 나올 때 알파벳/약어를 괄호로 함께 적습니다(예: 나스닥(NASDAQ), 연준(Fed), 연방공개시장위원회(FOMC), 소비자물가(CPI), 점도표(dot plot)).\n"
        "10) 실적 발표·경제지표 등 '시각이 정해진' 일정을 언급할 때는 항상 한국시간(KST)으로 환산해 원래 시간과 함께 병기합니다(예: '미 동부 오후 4시 30분(한국시간 익일 새벽 5시 30분)'). 미국은 서머타임 적용 여부에 따라 한국과의 시차가 13/14시간으로 달라지니, web_search로 해당 일정의 정확한 KST를 확인해 적습니다. 분 단위가 공시되지 않은 '장 마감 후(after market close)' 같은 표현은 그대로 옮기되, 콘퍼런스콜 등 구체 시각이 있으면 그 시각을 KST로 병기합니다.\n"
        "11) '역사적'이라는 표현은 해당 지표가 역사상 5위 이내임이 확인될 때만 사용하세요. 그 외에는 쓰지 말고 사실대로 순화합니다. '역대급'·'최근 수년간 손에 꼽히는'·'사상 최대' 같은 희소성·최상급 표현도 카운트나 공식 자료로 검증된 경우에만 쓰고, 아니면 쓰지 마세요. '어닝 쇼크'는 실적이 기대를 밑돈 경우에만 쓰고, 호실적 서프라이즈에는 쓰지 마세요(정반대 의미).\n"
        "12) 지수의 '급등/급락/약세/강세 출발(개장 기사)' 또는 '마감(마감 기사)'은 반드시 '금일 시초가' 또는 '금일 종가'의 실제 부호로만 판단해 적습니다. '전일 종가'의 등락률을 오늘의 개장/마감 등락률로 절대 재사용하지 마세요(예: 전일 종가 +5.42%를 '오늘 +5.42% 급등 출발'로 쓰면 안 됨). 표와 본문에서 '전일 종가'와 '금일 시초가/종가'를 항상 구분해 명시하고, 금일 시초가 데이터가 없으면 방향(급등/급락 등)을 단정하지 마세요.\n"
        "13) ===TITLE===(제목)과 ===SUBTITLE===(부제)에는 어떤 HTML 태그도 넣지 마세요. .up/.down/.key/.num 등 강조 span은 본문(===BODY===)에만 사용하고, 제목·부제는 순수 텍스트(숫자·기호는 그대로)로만 작성합니다.\n"
        "14) 기업·시장 이벤트 일정(실적발표·상장·공시·FOMC 등)의 '날짜'는 web_search로 교차 확인한 뒤에만 구체적으로 적습니다. ① 날짜에 요일을 병기할 때는 실제 달력과 일치하는지 반드시 확인하세요(날짜-요일 불일치는 자동 검산에 걸려 발행이 차단됩니다). ② 이벤트의 '성격'을 혼동하지 마세요 — 예: 상장일을 실적발표일로, 잠정실적 발표를 확정실적 발표로 쓰면 안 됩니다. ③ 날짜가 출처로 확인되지 않으면 구체 날짜·요일을 지어내지 말고 '이달 말 예정' 등으로 순화합니다.\n"
        "15) 미국 국채금리(10년물·30년물)가 실데이터로 제공되면 지수·지표표에 수록하고, 장기금리 레벨(예: 10년물 4.5%·30년물 5%)이 성장주·반도체 밸류에이션에 갖는 의미를 시황 분석에서 맥락화합니다. 금리 '레벨'에는 +/− 부호를 붙이지 않고, 일간 변동은 %p 또는 bp 단위로 표기합니다.\n\n"
        + ("16) [개장호 편집 원칙] 개장 브리핑의 주인공은 간밤(주말·연휴 후엔 주말간) 미국 증시입니다. 제목·부제·요약·리드 문단은 모두 미국 지수(나스닥·S&P500·다우)·대장주(엔비디아 등) 등락이나 간밤 최대 이슈를 앞세우고, 전일 한국 증시 마감(코스피·코스닥 급등/급락 수치)을 앞머리 주어로 삼지 마세요. 전일 국내 마감은 오늘 국내 증시에 미칠 영향의 맥락으로만 뒤에서 짧게 다룹니다.\n\n" if mode == "open" else "")
        + f"{AI_CLASSES}\n"
        + (("[긴급 정정 지시 — 최우선 준수] " + correction_note + "\n\n") if correction_note else "")
        + "[발행 전 자가점검] 출력하기 전에 본문을 스스로 점검하세요: ⑴ 모든 등락률(%)이 .up/.down으로 감싸졌는가, ⑵ 핵심 키워드·사건명에 .key 밑줄이 (기사 전체 6개 이상) 충분히 들어갔는가, ⑶ 중요한 절대수치(지수레벨·금액)에 .num을 썼는가, ⑷ 각 섹션이 .takeaway로 마무리됐는가, ⑸ '역사적·역대급·사상 최대' 등 미검증 최상급을 쓰지 않았는가(규칙 11), ⑹ '급등/약세 출발' 등 방향을 '전일 종가'가 아닌 '금일 시초가/종가'의 실제 부호로 적었는가(규칙 12), ⑺ 제목·부제에 HTML 태그(<span> 등)를 넣지 않았는가(규칙 13), ⑻ 본문의 모든 '날짜(요일)' 병기가 실제 달력과 일치하고 이벤트 성격(실적발표/상장/공시 등)을 원 출처 그대로 적었는가(규칙 14), ⑼ 확정 데이터로 제공된 미국 국채금리(10년물·30년물)를 지수·지표표와 시황 분석에 반영했는가(규칙 15). 하나라도 어긋나면 고쳐 다시 작성한 뒤 출력하세요.\n\n"
        "[출력 형식] 아래 형식 '그대로' 출력하세요. 각 구분선(===...===)을 정확히 쓰고 그 사이에 내용만 넣으세요. "
        "마크다운 코드펜스(```)나 형식 밖의 다른 말은 절대 쓰지 마세요. body_html은 위 클래스만 쓴 순수 HTML입니다.\n"
        "===TITLE===\n"
        + ("(기사 제목 20~45자, 핵심 수치 포함. [개장호 필수] 간밤(주말·연휴 후엔 주말간) 미국 증시를 주인공으로 — 미국 지수(나스닥·S&P500·다우)·대장주(엔비디아 등) 등락이나 간밤 최대 이슈를 제목 앞머리에 두고, 전일 한국 증시 마감(코스피·코스닥 수치)을 제목 주어로 쓰지 말 것)\n" if mode == "open" else "(기사 제목 20~45자, 핵심 수치 포함)\n")
        + "===SUBTITLE===\n"
        "(부제 한 줄 60~110자)\n"
        "===SUMMARY===\n"
        "(홈 미리보기용 요약 120~180자)\n"
        "===BODY===\n"
        "(<p class=\"lead-para\">…</p> 이하 본문 HTML 전체)"
    )


def call_anthropic(api_key, system, user, max_tokens=16000, max_searches=7, max_attempts=4):
    # 재시도 정책(사고#8 대응): 과부하·타임아웃·5xx·429 등 '일시적' 오류는 백오프 후 재시도.
    # 단 400(잔액부족 등)·401·403은 재시도해도 동일하므로 즉시 실패시켜 폴백 판단으로 넘긴다.
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
    }
    payload = json.dumps(body).encode("utf-8")
    last_err = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                     data=payload,
                                     headers={"content-type": "application/json",
                                              "x-api-key": api_key,
                                              "anthropic-version": "2023-06-01",
                                              "User-Agent": UA}, method="POST")
        try:
            # 사고#27·#28 대응: 폭락/폭등일 대용량 생성(web_search 포함)은 응답에 4분+ 소요될 수 있음.
            # 240초에서 600초로 상향 — 서버가 응답을 완성할 때까지 read를 기다린다.
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.load(r)
            texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            out = "\n".join(t for t in texts if t).strip()
            if not out:
                raise ValueError("빈 응답(텍스트 블록 없음)")
            return out
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):  # 잔액/인증/요청오류 → 재시도 무의미
                try:
                    detail = e.read().decode("utf-8", "ignore")[:300]
                except Exception:
                    detail = ""
                raise RuntimeError(f"Anthropic {e.code} 비재시도 오류(잔액/인증 점검): {detail}") from e
            last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_err = repr(e)
        if attempt < max_attempts:
            wait = min(45, 6 * (2 ** (attempt - 1)))  # 6 → 12 → 24 → (45)
            sys.stderr.write(f"[ai] Claude 호출 실패(시도 {attempt}/{max_attempts}: {last_err}) — {wait}s 후 재시도\n")
            time.sleep(wait)
    raise RuntimeError(f"Anthropic 호출 {max_attempts}회 연속 실패: {last_err}")


def parse_article(txt):
    """구분자(===TITLE=== 등) 기반 파서. HTML 따옴표와 충돌하지 않아 견고하다."""
    t = txt or ""
    M = ["===TITLE===", "===SUBTITLE===", "===SUMMARY===", "===BODY==="]
    pos = {m: t.find(m) for m in M}
    if any(v < 0 for v in pos.values()):
        raise ValueError("출력 구분자 누락(===TITLE/SUBTITLE/SUMMARY/BODY===)")

    def sect(m, nxt):
        s = pos[m] + len(m)
        e = pos[nxt] if nxt else len(t)
        return t[s:e].strip()

    title = re.sub(r'<[^>]+>', '', sect("===TITLE===", "===SUBTITLE===")).strip()
    subtitle = re.sub(r'<[^>]+>', '', sect("===SUBTITLE===", "===SUMMARY===")).strip()
    summary = sect("===SUMMARY===", "===BODY===")
    body = sect("===BODY===", None)
    # 혹시 코드펜스가 섞이면 제거
    if body.startswith("```"):
        body = body.lstrip("`")
        if body[:4].lower() == "html":
            body = body[4:]
        body = body.rstrip("`").strip()
    if not title or not body:
        raise ValueError("title/body 비어있음")
    return {"title": title, "subtitle": subtitle, "summary": summary, "body_html": body}


def compose_with_claude(api_key, mode, date_kst, items, asof, vol, rank_up, rank_dn, flows, us_movers=None, crypto=None, correction_note=None):
    system = (
        "당신은 한국의 데일리 투자 뉴스레터 '투자이야기(INVEST STORY)'의 증시 전문 기자입니다. "
        "기사는 '박철웅 기자' 명의로 공개 발행됩니다. 정확성과 출처 표기를 최우선으로 하며, 확인되지 않은 "
        "사실이나 과장된 추측은 쓰지 않습니다. 제공된 확정 수치는 그대로 쓰고, 그 외 사실·뉴스·종목 동향은 "
        "web_search로 직접 확인해 출처를 답니다. 한국 증시 색상 관례(상승=빨강, 하락=파랑)를 따릅니다. "
        "반드시 지정된 구분자 형식으로만 출력합니다."
    )
    user = _ai_user_prompt(mode, date_kst, items, asof, vol, rank_up, rank_dn, flows, us_movers, crypto, correction_note=correction_note)
    last_err = None
    for attempt in range(1, 3):  # 파싱 실패(형식 깨짐) 시 1회 더 재생성
        raw = call_anthropic(api_key, system, user)
        try:
            return parse_article(raw)
        except Exception as e:
            last_err = e
            sys.stderr.write(f"[ai] 응답 파싱 실패(시도 {attempt}/2): {e}. 원문 앞 400자:\n"
                             + (raw[:400] if raw else "(빈 응답)") + "\n")
    raise last_err





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
 :root{--navy:#1B3C6E;--navy-bar:#2E4B77;--gold:#C9A654;--gold-d:#a98731;--ink:#1F2933;--mute:#6B7785;--line:#E2E6EC;
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
 /* ===== 본문 강조 키트(색=역할 1:1) · 2026-06-23 추가 ===== */
 main .key{font-weight:700;color:var(--ink);background:linear-gradient(transparent 60%,rgba(201,166,84,.32) 60%);padding:0 1px;border-radius:1px}
 main .num{font-family:var(--serif);font-weight:700;color:var(--navy);font-variant-numeric:tabular-nums}
 main strong{font-weight:700;color:var(--ink);letter-spacing:-.01em}
 p.takeaway{margin:16px 0 6px;padding:12px 15px;background:#FBF7EE;border-left:4px solid var(--gold);border-radius:0 7px 7px 0;font-family:var(--serif);font-weight:700;color:var(--navy);font-size:15px;line-height:1.62}
 table.grid{width:100%;border-collapse:collapse;margin:6px 0 18px;font-size:13.5px;border:1px solid var(--line);table-layout:fixed}
 table.grid th{background:var(--navy-bar);color:#fff;font-weight:700;text-align:left;padding:9px 10px;font-size:12.5px;vertical-align:top}
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
 /* ===== 기사 요약 라벨(시안 C) + 모바일 표 호환 · 2026-06-22 추가 ===== */
 .art-hero + .lead-para{font-size:16px;line-height:1.78;border:1px solid #E2E6EC;border-radius:4px;background:#fff;padding:15px 18px 16px;margin:18px 0 24px;position:relative}
 .art-hero + .lead-para::before{content:"기사 요약";display:block;background:var(--navy-bar);color:#fff;font-weight:700;font-size:12px;letter-spacing:.12em;padding:9px 18px;margin:-15px -18px 13px;border-left:4px solid #C9A654;border-radius:4px 4px 0 0}
 table.grid td,table.grid th,table.kv td,table.kv th{overflow-wrap:anywhere}
 table.kv{table-layout:fixed}
 @media (max-width:560px){
  main{padding-left:14px;padding-right:14px}
  .art-hero{padding:20px 18px 22px}
  .art-hero + .lead-para{font-size:15px;padding:13px 14px 14px}
  .art-hero + .lead-para::before{padding:8px 14px;margin:-13px -14px 11px}
  table.grid,table.kv{font-size:12px}
  table.grid th,table.grid td,table.kv th,table.kv td{padding:7px 6px;line-height:1.5}
 }
 /* ===== 상세 목차(TOC) + 가독성 · 2026-06-22 ===== */
 .sec{scroll-margin-top:74px;margin-top:42px}
 #toc{margin:6px 0 22px;border:1px solid #E2E6EC;border-radius:6px;background:#fff;overflow:hidden}
 #toc .toc-h{display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:11px 15px;font-size:13px;font-weight:700;color:#1B3C6E;background:#F4F6F8;user-select:none}
 #toc .toc-h .tg{color:#a98731;font-size:12px}
 #toc ol{list-style:none;margin:0;padding:6px 6px 8px;counter-reset:toc}
 #toc li{counter-increment:toc}
 #toc a{display:flex;gap:9px;padding:7px 10px;border-radius:4px;font-size:13px;color:#34404e;line-height:1.4;text-decoration:none}
 #toc a:before{content:counter(toc,decimal-leading-zero);color:#C9A654;font-weight:700;font-variant-numeric:tabular-nums}
 #toc a:hover{background:#F4F6F8;color:#1B3C6E}
 #toc.collapsed ol{display:none}
 .toTop{position:fixed;right:18px;bottom:18px;width:42px;height:42px;border-radius:50%;background:#1B3C6E;color:#fff;border:none;cursor:pointer;font-size:18px;line-height:42px;box-shadow:0 3px 12px rgba(20,41,74,.3);opacity:0;pointer-events:none;transition:opacity .2s;z-index:30}
 .toTop.on{opacity:1;pointer-events:auto}
 @media (min-width:1280px){
  #toc{position:fixed;top:88px;left:max(16px,calc((100vw - 820px)/2 - 206px));width:190px;max-height:72vh;overflow:auto;margin:0;z-index:10}
  #toc .toc-h{cursor:default} #toc .toc-h .tg{display:none} #toc.collapsed ol{display:block}
 }
 @media (max-width:560px){ .toTop{width:38px;height:38px;line-height:38px;font-size:16px} }
</style>"""

HEAD_A = ('<!doctype html><html lang="ko"><head>\n'
          '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
          '<title>')
HEAD_B = (' · INVEST STORY</title>\n'
          '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
          '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&family=Noto+Serif+KR:wght@600;700;900&display=swap" rel="stylesheet">\n'
          '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">\n'
          + ART_CSS + '\n</head><body>\n')


def head_html(title_esc, og_block=""):
    return HEAD_A + title_esc + HEAD_B.replace('</title>\n', '</title>\n' + og_block, 1)

TOC_JS = r"""<script>
(function(){
 var main=document.querySelector('main'); if(!main) return;
 var secs=main.querySelectorAll('section.sec'); if(secs.length<2) return;
 var ol=document.createElement('ol');
 secs.forEach(function(s,i){ var h=s.querySelector('h2'); if(!h) return;
  var id=s.id||('sec-'+(i+1)); s.id=id;
  var li=document.createElement('li'); var a=document.createElement('a');
  a.href='#'+id; a.textContent=h.textContent.trim();
  a.addEventListener('click',function(e){ e.preventDefault();
   document.getElementById(id).scrollIntoView({behavior:'smooth',block:'start'});
   history.replaceState(null,'','#'+id); });
  li.appendChild(a); ol.appendChild(li); });
 if(!ol.children.length) return;
 var toc=document.createElement('nav'); toc.id='toc';
 var hd=document.createElement('div'); hd.className='toc-h';
 hd.innerHTML='<span>목차</span><span class="tg">\u25BE</span>';
 var tg=hd.querySelector('.tg');
 toc.appendChild(hd); toc.appendChild(ol);
 var anchor=main.querySelector('.lead-para')||main.querySelector('.art-hero');
 if(anchor&&anchor.parentNode){ anchor.parentNode.insertBefore(toc,anchor.nextSibling); }
 else { main.insertBefore(toc,main.firstChild); }
 var narrow=window.matchMedia('(max-width:1279px)');
 tg.textContent='\u25BE';
 hd.addEventListener('click',function(){ if(!narrow.matches) return; toc.classList.toggle('collapsed'); tg.textContent=toc.classList.contains('collapsed')?'\u25B8':'\u25BE'; });
 var top=document.createElement('button'); top.className='toTop'; top.setAttribute('aria-label','맨 위로'); top.textContent='\u2191';
 top.addEventListener('click',function(){ window.scrollTo({top:0,behavior:'smooth'}); });
 document.body.appendChild(top);
 window.addEventListener('scroll',function(){ top.classList.toggle('on', window.scrollY>600); },{passive:true});
})();
</script>
"""

FOOT = ('<footer class="artfoot">\n'
        ' <a class="kk" href="https://open.kakao.com/o/giw7dfAb" target="_blank" rel="noopener">투자이야기 오픈채팅 바로가기</a><br>\n'
        ' <a class="back" href="/">\u2190 다른 리포트 보러가기</a>\n'
        ' <div class="dom">investstory.co.kr</div>\n'
        '</footer>\n' + TOC_JS + '</body></html>')


# 표에 쓸 표시 라벨(단위 병기). ticker.json의 name은 짧게 유지하고 표기만 여기서 늘린다.
INDEX_LABEL = {
    "KRX 금": "KRX 금 (원/g)",
    "국제 금": "국제 금 (현물, $/oz)",
}


def index_table(items, header):
    # 2026-08-24: 금 2종(KRX 금현물·국제 현물) 상시 수록.
    order = ["KOSPI", "KOSDAQ", "USD/KRW", "WTI", "S&P 500", "나스닥", "달러인덱스",
             "KRX 금", "국제 금"]
    rows = []
    for nm in order:
        it = items.get(nm)
        if not it:
            continue
        c = cls(it.get("dir", ""))
        rows.append(f'<tr><td>{esc(INDEX_LABEL.get(nm, nm))}</td><td>{esc(it.get("value","—"))}</td>'
                    f'<td class="{c}">{esc(it.get("change","—"))}</td></tr>')
    if not rows:
        return ""
    note = ""
    if items.get("KRX 금") or items.get("국제 금"):
        note = ('<p class="small">금 시세는 한국거래소 금시장 \'금 99.99_1Kg\'(원/그램)와 '
                '런던 현물 XAU/USD(달러/트로이온스) 기준입니다. 단위와 통화가 서로 다르므로 '
                '두 수치를 직접 비교할 수 없습니다.</p>')
    return (f'<table class="grid"><colgroup><col style="width:40%"><col style="width:32%"><col style="width:28%"></colgroup>'
            f'<thead><tr><th>{esc(header)}</th><th>지수/가격</th><th>등락</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>' + note)


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


def enrich_highlights(body_html):
    """강조 누락 안전망: 본문 산문에 들어간 '맨몸 등락률(±%)'을 자동으로 .up/.down 으로 감싼다.
    - SVG(<svg>…</svg>)·표(<table>…</table>)·기존 <span>…</span> 안은 절대 건드리지 않는다(차트 파손·중복 래핑·표 훼손 방지).
    - 부호 없는 값(기준금리·물가율·지수 레벨 등)은 손대지 않는다(부호 있는 일간 등락률만 대상)."""
    if not body_html:
        return body_html
    protect = re.compile(r'(<svg\b.*?</svg>|<table\b.*?</table>|<span\b.*?</span>)', re.S | re.I)
    # 부호(+/-/−)로 시작하는 퍼센트 토큰. 앞이 단어문자면(범위 '15%' 등) 제외.
    pct = re.compile(r'(?<![\w.])([+\-−]\d[\d,]*(?:\.\d+)?\s?%)')

    def wrap(seg):
        def repl(m):
            tok = m.group(1)
            cls = "down" if tok.lstrip()[0] in "-−" else "up"
            return f'<span class="{cls}">{tok}</span>'
        return pct.sub(repl, seg)

    parts = protect.split(body_html)        # [free, protected, free, protected, ...]
    for i in range(0, len(parts), 2):       # 보호 블록(홀수 인덱스)은 건너뜀
        parts[i] = wrap(parts[i])
    return "".join(parts)


def render_ai(mode, date_kst, meta):
    """Claude가 쓴 {title, subtitle, summary, body_html}을 기존 디자인에 입힌다."""
    d = date_kst
    dstr = f'{d.year}년 {d.month}월 {d.day}일({WEEKDAY_KR[d.weekday()]})'
    if mode == "breaking":
        tag = "특보"
        eyebrow = "JOSH PARK INVEST · 특보"
        toplabel = "특보"
    else:
        tag = "마감" if mode == "close" else "개장"
        eyebrow = "JOSH PARK INVEST · 데일리 " + ("마감 시황" if mode == "close" else "개장 브리핑")
        toplabel = "데일리 · " + tag
    title = re.sub(r'<[^>]+>', '', str(meta["title"])).strip()
    subtitle = re.sub(r'<[^>]+>', '', str(meta.get("subtitle", ""))).strip()
    body_html = enrich_highlights(str(meta["body_html"]))
    disc = ('본 리포트는 시장 정보 제공 및 분석 목적이며 특정 종목의 매수·매도 권유가 아닙니다. 본문의 뉴스·수치는 '
            '작성 시점 web 검색 및 공개 데이터를 근거로 하며 출처를 표기했으나, 속보성 사안은 이후 정정될 수 있습니다. '
            '전망·시나리오는 작성 시점 판단으로 실제와 다를 수 있습니다. 모든 투자 결정은 투자자 본인의 판단과 책임 '
            '하에 이루어져야 하며, Josh Park Invest는 본 자료를 활용한 투자 결과에 어떠한 책임도 지지 않습니다.')
    og_desc = esc(str(meta.get("summary") or subtitle or title)[:200])
    og_url = f"https://investstory.co.kr/newsletters/{d.strftime('%Y-%m-%d')}-{mode}.html"
    og_block = ('<meta property="og:type" content="article">\n'
                '<meta property="og:site_name" content="INVEST STORY">\n'
                f'<meta property="og:title" content="{esc(title)}">\n'
                f'<meta property="og:description" content="{og_desc}">\n'
                f'<meta property="og:url" content="{og_url}">\n'
                '<meta property="og:image" content="https://investstory.co.kr/assets/og-default.png">\n'
                '<meta property="og:image:width" content="1200">\n'
                '<meta property="og:image:height" content="630">\n'
                '<meta name="twitter:card" content="summary_large_image">\n'
                '<meta name="twitter:image" content="https://investstory.co.kr/assets/og-default.png">\n')
    parts = [head_html(esc(title), og_block)]
    parts.append(f'<div class="topbar"><div class="topbar-in"><a class="home" href="/">INVEST STORY</a>'
                 f'<span class="tag">{toplabel} · {d.strftime("%Y-%m-%d")}</span></div></div>\n')
    parts.append('<main>\n')
    parts.append(f'<header class="art-hero"><div class="ah-k">{esc(eyebrow)}</div>'
                 f'<div class="ah-t">{esc(title)}</div>'
                 f'<div class="ah-s">{esc(dstr)}{(" · " + esc(subtitle)) if subtitle else ""}</div></header>\n')
    parts.append(body_html)
    parts.append(f'<hr class="rule"><p class="disc">{disc}</p>')
    parts.append('<p class="byline" style="margin:30px 0 4px;padding-top:16px;border-top:1px solid var(--line);'
                 'font-weight:700;color:var(--ink);font-size:14px">박철웅 기자 '
                 '<a href="mailto:joshpark.korea@gmail.com" style="font-weight:600">joshpark.korea@gmail.com</a></p>\n')
    parts.append('</main>\n')
    parts.append(FOOT)
    summary = str(meta.get("summary") or subtitle)[:180]
    return "".join(parts), title, summary



# ----------------------------- 지수 검산 가드(사고#12 재발 방지) -----------------------------
def _g_num(s):
    try:
        return float(str(s).replace(",", "").replace("\u2212", "-").replace("%", "").replace("$", "").strip())
    except Exception:
        return None

# 실데이터 종가와 본문 수치의 허용 편차. 잠정↔확정 종가 괴리(통상 0.01% 미만)는
# 흡수하되, 전혀 다른 지수 수치(오보)는 걸러내는 폭으로 잡는다.
IDX_TOL = 0.003    # 0.30%  (코스피 6,900 기준 약 21포인트)


def verify_index_figures(htmlstr, items, asof):
    """마감 기사 본문의 KOSPI/KOSDAQ 수치를 ticker(KIS) 실데이터와 검산.
    (A) 실데이터 종가가 본문에 존재하는지(전체), (B) 리드 문단의 지수 인근 수치가
    실데이터(종가·전일종가·등락포인트) 화이트리스트와 일치하는지 검사.
    반환: (ok, issues). ticker가 오늘자가 아니면 검산 생략(경고만)."""
    issues = []
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if today not in str(asof):
        return True, [f"(경고) ticker asof({asof})가 오늘자가 아니라 검산을 건너뜀"]
    # 화이트리스트: 각 지수의 종가·(역산)전일종가·등락포인트
    white = []
    auth = {}
    for name in ("KOSPI", "KOSDAQ"):
        it = items.get(name) or {}
        ap = _g_num(it.get("value")); ac = _g_num(it.get("change"))
        if ap is None:
            continue
        auth[name] = (ap, ac)
        white.append(ap)
        if ac is not None and abs(1 + ac / 100) > 1e-6:
            prev = ap / (1 + ac / 100)
            white.append(prev); white.append(abs(ap - prev))
    if not auth:
        return True, ["(경고) ticker에 KOSPI/KOSDAQ 실데이터가 없어 검산 생략"]
    # (A) 존재 검사 — 실데이터 종가와 '충분히 가까운' 수치가 본문에 있으면 통과.
    # 사고#34(2026-08-27) 대응: 마감 발행(15:35)은 KRX 종가 확정 직후라, KIS가 아직
    # 잠정 종가를 주는 순간이 있다(8/27 KOSPI 잠정 6,911.70 → 확정 6,912.37, 편차 0.0097%).
    # AI는 웹에서 확인한 '확정' 종가를 쓰므로, 정확한 문자열 일치를 요구하면
    # 오히려 더 정확한 기사가 차단된다. 편차 IDX_TOL 이내면 확정 종가 반영으로 보고 통과시킨다.
    # 완화한 만큼 (C) 방향 검사로 사고#12(코스닥 방향 오류) 차단력을 보강한다.
    body_nums = [_g_num(x) for x in re.findall(r"\d{1,3}(?:,\d{3})*\.\d{2}", htmlstr)]
    body_nums = [v for v in body_nums if v is not None and v >= 50]
    for name, (ap, ac) in auth.items():
        pstr = f"{ap:,.2f}"
        if pstr in htmlstr:
            continue
        near = [v for v in body_nums if abs(v - ap) / max(ap, 1e-9) <= IDX_TOL]
        if near:
            best = min(near, key=lambda v: abs(v - ap))
            issues.append(f"(경고) {name}: 실데이터 {pstr} 대신 {best:,.2f} 표기 — "
                          f"편차 {abs(best - ap) / ap * 100:.3f}%, 확정 종가 반영으로 보고 통과")
            continue
        issues.append(f"{name}: 본문에 실데이터 종가 {pstr}에 근접한 수치가 없음"
                      f"(허용오차 {IDX_TOL * 100:.2f}%) — 웹 검색 수치로 대체됐을 가능성")
    # (B) 근접 검사 — 리드 문단 한정
    mlead = re.search(r'<p class="lead-para">(.*?)</p>', htmlstr, re.S)
    if mlead:
        lead = re.sub(r"<[^>]+>", " ", mlead.group(1))
        for name, kws in (("KOSPI", ("코스피", "KOSPI")), ("KOSDAQ", ("코스닥", "KOSDAQ"))):
            if name not in auth:
                continue
            for kw in kws:
                for m in re.finditer(re.escape(kw), lead):
                    seg = lead[m.end(): m.end() + 90]
                    for cm in re.finditer(r"(\d{1,3}(?:,\d{3})*\.\d{2})", seg):
                        tail = seg[cm.end(): cm.end() + 1]
                        if tail in ("원", "%", "달", "p", "P"):
                            continue  # 종목가·퍼센트·포인트 표기 등 지수 레벨이 아닌 수치
                        cv = _g_num(cm.group(1))
                        if cv is None or cv < 50:
                            continue
                        if not any(abs(cv - w) / max(w, 1e-9) <= 0.005 for w in white):
                            issues.append(f"{name}: 리드의 '{kw}' 인근 수치 {cm.group(1)} — 실데이터와 불일치")
    # (C) 방향 검사 — 리드의 지수 등락률 부호가 실데이터와 반대면 차단(사고#12).
    # 등락 크기가 실데이터와 사실상 같은데 방향만 뒤집힌 경우만 잡는다.
    if mlead:
        for name, kws in (("KOSPI", ("코스피", "KOSPI")), ("KOSDAQ", ("코스닥", "KOSDAQ"))):
            if name not in auth:
                continue
            ap, ac = auth[name]
            if ac is None or abs(ac) < 0.01:
                continue
            for kw in kws:
                for m in re.finditer(re.escape(kw), lead):
                    seg = lead[m.end(): m.end() + 80]
                    pm = re.search(r"([+\-\u2212]?)\s*(\d+\.\d{1,2})\s*%", seg)
                    if not pm:
                        continue
                    mag = _g_num(pm.group(2))
                    if mag is None or abs(mag - abs(ac)) > 0.05:
                        continue          # 지수 등락률이 아닌 다른 수치 — 건너뜀
                    sign = pm.group(1)
                    if sign in ("-", "\u2212"):
                        body_dir = -1
                    elif sign == "+":
                        body_dir = 1
                    else:
                        # 부호가 없으면 '그 수치 바로 옆'의 서술어로만 방향을 판정한다.
                        # 문장 전체를 훑으면 뒤에 붙은 다른 지수의 서술어를 잘못 집는다
                        # (예: "코스피는 1.53% 상승한 …, 코스닥도 1.24% 하락해").
                        DOWN = r"(하락|내린|내려|밀린|밀려|급락|약세|떨어|하회|후퇴)"
                        UP = r"(상승|오른|올라|뛴|뛰어|급등|강세|상회|반등)"
                        after = seg[pm.end(): pm.end() + 12]
                        before = seg[max(0, pm.start() - 12): pm.start()]
                        body_dir = 0
                        for win in (after, before):
                            dn_ = re.search(DOWN, win)
                            up_ = re.search(UP, win)
                            if dn_ and (not up_ or dn_.start() < up_.start()):
                                body_dir = -1; break
                            if up_:
                                body_dir = 1; break
                        if body_dir == 0:
                            continue
                    if body_dir * ac < 0:
                        issues.append(
                            f"{name}: 리드 등락률 {sign}{mag}% 방향이 실데이터 {ac:+.2f}%와 반대 — 방향 오기")
                    break

    hard = [x for x in issues if not x.startswith("(경고)")]
    return (len(hard) == 0), issues


def fix_weekday_mismatches(htmlstr, base_date):
    """요일'만' 틀린 날짜 병기를 실제 달력 요일로 결정적 교정(2026-07-08 신설).
    재생성 피드백으로도 AI의 요일 계산 오류가 반복(두더지 게임)돼, 마지막 단계에서
    괄호 속 요일 글자만 계산값으로 치환한다. 날짜 자체·이벤트 서술은 건드리지 않으며,
    존재하지 않는 날짜는 교정하지 않는다(검산이 그대로 차단). 반환: (html, 교정목록)."""
    fixes = []
    pat = re.compile(
        r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일\s*"
        r"(?:\(([월화수목금토일])\)|([월화수목금토일])요일)")

    def _actual(y, mo, dd):
        cand_years = [int(y)] if y else [base_date.year - 1, base_date.year, base_date.year + 1]
        best = None
        for cy in cand_years:
            try:
                dt = datetime(cy, mo, dd)
            except ValueError:
                continue
            gap = abs((dt.date() - base_date.date()).days)
            if (y or gap <= 200) and (best is None or gap < best[0]):
                best = (gap, dt)
        return best[1] if best else None

    def _sub(m):
        y, mo, dd = m.group(1), int(m.group(2)), int(m.group(3))
        stated = m.group(4) or m.group(5)
        # 후보 연도 중 하나라도 표기 요일과 일치하면 정상 → 유지(검산과 동일한 관용)
        cand_years = [int(y)] if y else [base_date.year - 1, base_date.year, base_date.year + 1]
        for cy in cand_years:
            try:
                dt = datetime(cy, mo, dd)
            except ValueError:
                continue
            if (y or abs((dt.date() - base_date.date()).days) <= 200) and WEEKDAY_KR[dt.weekday()] == stated:
                return m.group(0)
        dt = _actual(y, mo, dd)
        if dt is None:
            return m.group(0)  # 존재하지 않는 날짜 등 — 교정 불가(검산이 차단)
        correct = WEEKDAY_KR[dt.weekday()]
        fixes.append(f"'{m.group(0).strip()}' → 요일 '{stated}'을 '{correct}'로 자동 정정({dt.year}-{mo:02d}-{dd:02d})")
        if m.group(4):
            return m.group(0).replace(f"({stated})", f"({correct})")
        return m.group(0).replace(f"{stated}요일", f"{correct}요일")

    return pat.sub(_sub, htmlstr), fixes


def _valid_date(y, mo, dd):
    try:
        datetime(y, mo, dd); return True
    except ValueError:
        return False


def verify_event_weekdays(htmlstr, base_date):
    """본문의 'M월 D일(요일)' / 'M월 D일 요일' 병기가 실제 달력과 일치하는지 검산.
    (사고#14: SK하이닉스 '7월 10일 목요일 실적발표' 오보 — 실제 10일은 금요일이며
    해당일은 나스닥 ADR 상장일. 요일 불일치는 AI가 일정을 지어냈다는 강한 신호다.)
    연도 미표기 시 발행일 연도를 쓰되, 연말·연초 경계는 ±6개월 근접 연도로 해석.
    반환: (ok, issues)."""
    issues = []
    text = re.sub(r"<[^>]+>", " ", htmlstr)
    pat = re.compile(
        r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일\s*"
        r"(?:\(([월화수목금토일])\)|([월화수목금토일])요일)")
    for m in pat.finditer(text):
        y, mo, dd = m.group(1), int(m.group(2)), int(m.group(3))
        wk = m.group(4) or m.group(5)
        cand_years = [int(y)] if y else [base_date.year - 1, base_date.year, base_date.year + 1]
        dts = []
        for cy in cand_years:
            try:
                dt = datetime(cy, mo, dd)
            except ValueError:
                continue
            gap = abs((dt.date() - base_date.date()).days)
            if y or gap <= 200:  # 연도 명시 시 그대로, 미표기 시 근접 후보만(반년 경계 ±약 3주 관용)
                dts.append((gap, dt))
        if not dts:
            if y:  # 연도까지 명시했는데 달력에 없는 날짜
                issues.append(f"존재하지 않는 날짜: '{m.group(0).strip()}'")
            elif not any(True for cy in cand_years
                         for _ in [0] if _valid_date(cy, mo, dd)):
                issues.append(f"존재하지 않는 날짜: '{m.group(0).strip()}'")
            continue  # 원거리 날짜는 연도 추정이 불확실해 판정 보류
        # 연도 미표기 시 후보 중 '하나라도' 요일이 맞으면 통과(모호성은 관용)
        if any(WEEKDAY_KR[dt.weekday()] == wk for _, dt in dts):
            continue
        dts.sort()
        dt = dts[0][1]
        issues.append(
            f"날짜-요일 불일치: '{m.group(0).strip()}' — {dt.year}년 {mo}월 {dd}일의 실제 요일은 "
            f"'{WEEKDAY_KR[dt.weekday()]}'. 일정 자체가 잘못됐을 가능성(웹 검색 재확인 필요)")
    return (len(issues) == 0), issues

# ----------------------------- manifest / build -----------------------------
def update_manifest(date_str, time_str, mode, title, summary, relfile, tag="데일리"):
    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    issues = man.setdefault("issues", [])
    # 같은 파일(=같은 날짜·모드)이면 갱신
    existing = next((it for it in issues if it.get("file") == relfile), None)
    if existing:
        existing.update({"date": date_str, "time": time_str, "tag": tag,
                         "title": title, "summary": summary})
        no = existing.get("no")
    else:
        no = max([it.get("no", 0) for it in issues] + [0]) + 1
        issues.append({"no": no, "date": date_str, "time": time_str, "tag": tag,
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
    print("ANTHROPIC_API_KEY:", "있음" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "없음(→템플릿 모드)")
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
        vol = fetch_value_rank(token, key, sec, 3)
        print("거래대금 상위:", "OK " + ", ".join(x["name"] for x in vol) if vol else "실패(섹션 생략됨)")
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
    # 발행 시간 가드: '마감'은 15:00 이후에만(13:17 같은 조기 마감 방지). 예약 15:35은 정상 통과.
    # 개장 브리핑은 당일 재실행 허용(데이터 보정 목적). --force로 우회 가능.
    if not force and mode == "close" and now.hour < 15:
        print(f"[daily_news] 마감 기사는 15:00 KST 이후에만 발행합니다(현재 {now:%H:%M}). 발행하지 않고 종료. (--force로 강제 가능)")
        return 0
    ok, why = (True, "강제") if force else trading_day(now, token, key, sec)
    print(f"[daily_news] {now:%Y-%m-%d %H:%M KST} · mode={mode} · 거래일판정={ok}({why})")
    if not ok:
        print("[daily_news] 휴장일 — 생성하지 않고 종료")
        return 0

    items, asof = load_ticker()
    if not items:
        sys.stderr.write("[daily_news] ticker.json 비어있음 — update_ticker.py를 먼저 실행하세요\n")
        return 1
    try:  # 미국 국채금리 상시 수록(독자 피드백 반영, 2026-07-07) — 실패해도 발행 영향 없음
        items.update(fetch_us_yields())
    except Exception as _e:
        sys.stderr.write(f"[yh] 국채금리 수집 실패(계속 진행): {_e}\n")

    if mode == "open":
        dji = fmp_index("^DJI", os.environ.get("FMP_API_KEY", "").strip()) or yahoo_index("^DJI")
        if dji:
            items["다우존스"] = dji
            print(f"[daily_news] 다우존스 종가 확보: {dji['value']}({dji['change']})")
        else:
            print("[daily_news] 다우존스 종가 미확보(FMP·야후) — AI가 web_search로 채움")

    rank_up = rank_dn = flows = vol = us_movers = crypto = None
    if mode == "open":
        _fk = os.environ.get("FMP_API_KEY", "").strip()
        _base = fetch_us_movers(_fk, 6) or fetch_us_movers_free(6)   # FMP 실패 시 무료(야후) 경로
        _news = fetch_most_mentioned_us(_fk) or fetch_trending_us()   # FMP 뉴스 실패 시 야후 트렌딩
        us_movers = combine_us_focus(_base, _news, 5)   # 거래대금4 + 픽1(겹치면 거래대금5위)
        print(f"[daily_news] 미국주목종목={'O' if us_movers else 'X'} "
              f"(거래대금4+픽1, 픽={_news.get('symbol') if _news else '-'})")
    crypto = fetch_crypto_movers(os.environ.get("TWELVEDATA_API_KEY", "").strip())
    print(f"[daily_news] 암호화폐={'O' if crypto else 'X'}")
    update_kimchi_history(crypto, mode)
    if token:
        if mode == "close":
            topcap = fetch_topcap_codes(token, key, sec, 100)   # 시총 상위 100위 집합
            rank_up = fetch_fluctuation(token, key, sec, "0", 5, allow=topcap)
            rank_dn = fetch_fluctuation(token, key, sec, "1", 5, allow=topcap)
            vol = fetch_value_rank(token, key, sec, 5)
            flows = fetch_investors(token, key, sec)
            print(f"[daily_news] 시총상위100={'O' if topcap else 'X'} 거래대금상위={'O' if vol else 'X'} 등락상위={'O' if rank_up else 'X'} "
                  f"등락하위={'O' if rank_dn else 'X'} 수급={'O' if flows else 'X'}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    htmlstr = title = summary = None
    if api_key:
        try:
            meta = compose_with_claude(api_key, mode, now, items, asof, vol, rank_up, rank_dn, flows, us_movers, crypto)
            try:
                meta["body_html"] = inject_stock_tables(meta["body_html"], token, key, sec)
            except Exception as e:
                sys.stderr.write(f"[stock] 종목표 삽입 경고: {e}\n")
            try:
                meta["body_html"] = inject_crypto_table(meta["body_html"], crypto)
            except Exception as e:
                sys.stderr.write(f"[crypto] 코인표 삽입 경고: {e}\n")
            try:
                meta["body_html"] = inject_charts(meta["body_html"], mode, items, rank_up, rank_dn, vol, us_movers, crypto)
            except Exception as e:
                sys.stderr.write(f"[charts] 자동 차트 삽입 경고: {e}\n")
            try:
                meta["body_html"] = inject_section_cards(meta["body_html"])
            except Exception as e:
                sys.stderr.write(f"[cards] 섹션 요약 카드 삽입 경고: {e}\n")
            htmlstr, title, summary = render_ai(mode, now, meta)
            print(f"[daily_news] Claude API 작성 완료 · 제목: {title}")
        except Exception as e:
            sys.stderr.write(f"[ai] Claude 작성 실패 — 템플릿으로 폴백: {e}\n")
    else:
        print("[daily_news] ANTHROPIC_API_KEY 없음 — 템플릿 모드(숫자 위주)")

    used_ai = htmlstr is not None
    if htmlstr is None:
        htmlstr, title, summary = build_html(mode, now, items, asof, rank_up, rank_dn, flows)

    os.makedirs(NEWS_DIR, exist_ok=True)
    fname = f'{now:%Y-%m-%d}-{mode}.html'
    relfile = f'newsletters/{fname}'
    abspath = os.path.join(NEWS_DIR, fname)

    # ── 수동 확정 기사 보호 (재발 방지) ───────────────────────────────
    # 증상: 지연·중복 크론으로 같은 날 close가 한 번 더 돌면, 손본 긴 기사를
    #       짧은 자동본으로 덮어써 버린다(예: 23:45 재실행).
    prev = None
    if os.path.exists(abspath):
        try:
            with open(abspath, encoding="utf-8") as _pf:
                prev = _pf.read()
        except Exception:
            prev = None
    if prev is not None:
        # (1) 잠금: <!-- INVEST_STORY_LOCKED --> 가 있으면 자동발행이 절대 덮어쓰지 않음(수동 확정본 보호)
        if "INVEST_STORY_LOCKED" in prev:
            print(f"[lock] {relfile} 잠금(LOCKED) — 재생성·manifest 갱신을 건너뜁니다(수동 확정본 보호).")
            return 0
        force = os.environ.get("FORCE_REGEN", "").strip() == "1"
        # (2) 멱등성: 같은 날짜·모드 파일이 이미 있으면 중복/지연 재실행으로 보고 건너뜀
        if not force:
            print(f"[guard] {relfile} 이미 존재 — 중복/지연 재실행으로 판단해 건너뜁니다. "
                  f"강제로 덮어쓰려면 FORCE_REGEN=1 환경변수를 주세요.")
            return 0
        # (3) 폴백 보호: 강제 재생성이어도 AI 실패(짧은 템플릿)면 더 긴 기존 파일을 보존
        if (not used_ai) and len(htmlstr) < len(prev):
            print(f"[guard] 강제 재생성이나 AI 작성 실패(템플릿) — 더 긴 기존 파일을 보존합니다: {relfile}")
            return 0

    # ── 품질 게이트(사고#8 재발 방지) ──────────────────────────────
    # 개장/마감은 '분석 본문'이 핵심이라, 재시도로도 AI가 못 살아난 경우(주로 ANTHROPIC 잔액 소진)
    # 짧은 템플릿(숫자만)을 그대로 자동발행하지 않는다. 런을 실패(비0)시켜 운영자에게 알리고,
    # 수동 상세본(인수인계서 12.3)으로 가게 한다. 의도된 템플릿 발행/테스트는 ALLOW_TEMPLATE=1.
    allow_template = os.environ.get("ALLOW_TEMPLATE", "").strip() == "1"
    if (not used_ai) and mode in ("open", "close") and not allow_template:
        sys.stderr.write(
            f"[ALERT] AI 작성 실패 → {mode} 템플릿(간단본) 자동발행 중단({now:%Y-%m-%d}). "
            "수동 상세본 발행 필요(인수인계서 12.3). "
            "원인 점검: ANTHROPIC_API_KEY 잔액/일시오류. "
            "의도된 템플릿 발행이면 ALLOW_TEMPLATE=1 로 재실행.\n")
        return 2

    # ── 지수 검산 게이트(사고#12: 42호 코스닥 방향 오류 재발 방지) ─────────
    # 마감 기사는 KOSPI/KOSDAQ 수치가 ticker(KIS) 실데이터와 일치해야 발행된다.
    # 불일치 시 발행을 중단(비0)하고 로그로 상세를 남긴다. 우회: ALLOW_UNVERIFIED=1
    if used_ai and mode == "close" and os.environ.get("ALLOW_UNVERIFIED", "").strip() != "1":
        _ok, _issues = verify_index_figures(htmlstr, items, asof)
        for _msg in _issues:
            print(f"[verify] {_msg}")
        if not _ok:
            sys.stderr.write(
                f"[ALERT] 지수 검산 실패 → {mode} 발행 중단({now:%Y-%m-%d}). "
                "본문 KOSPI/KOSDAQ 수치가 KIS/ticker 실데이터와 불일치. "
                "확인 후 재실행하거나, 의도된 경우 ALLOW_UNVERIFIED=1 로 우회.\n")
            return 2

    # ── 날짜-요일 검산 게이트(사고#14: SK하이닉스 일정 오보 재발 방지) ─────────
    # 본문의 '날짜(요일)' 병기가 실제 달력과 불일치하면 일정 자체를 지어냈을
    # 가능성이 높으므로 발행을 중단한다(개장·마감 공통). 우회: ALLOW_UNVERIFIED=1
    if used_ai and os.environ.get("ALLOW_UNVERIFIED", "").strip() != "1":
        _ok2, _issues2 = verify_event_weekdays(htmlstr, now)
        for _msg in _issues2:
            print(f"[verify] {_msg}")
        if not _ok2:
            # ── 자동 재생성 1회(사고#14 만성화 대응, 2026-07-08) ─────────────
            # 같은 요일 오류가 3일 연속 반복 → 규칙만으로는 부족. 검산이 잡아낸
            # 오류 내용을 '정정 지시'로 프롬프트에 되먹여 1회 재작성한다.
            print("[verify] 날짜-요일 검산 실패 — 오류 피드백을 포함해 1회 자동 재생성 시도")
            _note = ("직전 초안에서 다음 날짜-요일 오류가 확인되었습니다: " + " / ".join(_issues2)
                     + " — 해당 일정의 실제 날짜·요일과 이벤트 성격(실적발표/상장/공시 등)을 web_search로 "
                       "재확인해 바로잡고, 확인되지 않으면 구체 날짜·요일 표기를 제거하세요. "
                       "그 외 본문 구성과 품질은 그대로 유지합니다.")
            try:
                _meta2 = compose_with_claude(api_key, mode, now, items, asof, vol, rank_up,
                                             rank_dn, flows, us_movers, crypto, correction_note=_note)
                try:
                    _meta2["body_html"] = inject_stock_tables(_meta2["body_html"], token, key, sec)
                except Exception as _e:
                    sys.stderr.write(f"[regen] 종목표 경고: {_e}\n")
                try:
                    _meta2["body_html"] = inject_crypto_table(_meta2["body_html"], crypto)
                except Exception as _e:
                    sys.stderr.write(f"[regen] 코인표 경고: {_e}\n")
                try:
                    _meta2["body_html"] = inject_charts(_meta2["body_html"], mode, items, rank_up, rank_dn, vol, us_movers, crypto)
                except Exception as _e:
                    sys.stderr.write(f"[regen] 차트 경고: {_e}\n")
                try:
                    _meta2["body_html"] = inject_section_cards(_meta2["body_html"])
                except Exception as _e:
                    sys.stderr.write(f"[regen] 카드 경고: {_e}\n")
                _h2, _t2, _s2 = render_ai(mode, now, _meta2)
                _rok, _riss = verify_event_weekdays(_h2, now)
                for _msg in _riss:
                    print(f"[verify] (재생성) {_msg}")
                if _rok and mode == "close":
                    _rok, _riss_i = verify_index_figures(_h2, items, asof)
                    for _msg in _riss_i:
                        print(f"[verify] (재생성) {_msg}")
                if not _rok:
                    # ── 최후 교정: 요일 글자만 결정적으로 정정(날짜·서술 불변) ──
                    _fixed, _fixes = fix_weekday_mismatches(_h2, now)
                    if _fixes:
                        for _f in _fixes:
                            print(f"[verify-fix] {_f}")
                        _rok, _riss3 = verify_event_weekdays(_fixed, now)
                        for _msg in _riss3:
                            print(f"[verify] (교정 후) {_msg}")
                        if _rok and mode == "close":
                            _rok, _riss3i = verify_index_figures(_fixed, items, asof)
                            for _msg in _riss3i:
                                print(f"[verify] (교정 후) {_msg}")
                        if _rok:
                            _h2 = _fixed
                            print("[daily_news] 요일 자동 정정 후 검산 통과")
                if _rok:
                    htmlstr, title, summary = _h2, _t2, _s2
                    _ok2 = True
                    print(f"[daily_news] 재생성 성공 · 제목: {title}")
            except Exception as _e:
                sys.stderr.write(f"[regen] 재생성 실패: {_e}\n")
            # 재생성 자체가 예외로 죽은 경우: 원본에라도 요일 교정을 시도
            if not _ok2:
                _fixed0, _fixes0 = fix_weekday_mismatches(htmlstr, now)
                if _fixes0:
                    for _f in _fixes0:
                        print(f"[verify-fix] (원본) {_f}")
                    _rok0, _riss0 = verify_event_weekdays(_fixed0, now)
                    if _rok0 and mode == "close":
                        _rok0, _ = verify_index_figures(_fixed0, items, asof)
                    if _rok0:
                        htmlstr = _fixed0
                        _ok2 = True
                        print("[daily_news] (원본) 요일 자동 정정 후 검산 통과")
        if not _ok2:
            sys.stderr.write(
                f"[ALERT] 날짜-요일 검산 실패(재생성 포함) → {mode} 발행 중단({now:%Y-%m-%d}). "
                "본문에 실제 달력과 다른 요일 병기가 있음(일정 오보 가능성). "
                "확인 후 재실행하거나, 의도된 경우 ALLOW_UNVERIFIED=1 로 우회.\n")
            return 2

    with open(abspath, "w", encoding="utf-8") as f:
        f.write(htmlstr)
    print(f"[daily_news] 기사 작성: {relfile}")

    no = update_manifest(now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), mode, title, summary, relfile)
    print(f"[daily_news] manifest 등록: 제{no}호 (데일리)")

    if rebuild_site():
        print("[daily_news] index.html 재생성 완료")
    else:
        sys.stderr.write("[daily_news] build_site 실패\n"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
