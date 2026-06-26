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
                "code": o.get("stck_shrn_iscd") or o.get("mksc_shrn_iscd") or "",
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


def fetch_volume_rank(token, app_key, app_secret, n=5):
    """국내 거래량 상위 종목 — best-effort. 실패 시 None."""
    try:
        params = {
            "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20171",
            "fid_input_iscd": "0000", "fid_div_cls_code": "0", "fid_blng_cls_code": "0",
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
                "sign": o.get("prdy_vrss_sign", "3"),
            })
        return out or None
    except Exception as e:
        sys.stderr.write(f"[vol] 거래량순위 실패: {e}\n")
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
        # 두 소스 모두 실패해 빈 표가 될 종목 경고(아침에 바로 눈에 띄게)
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
                kr_cache[ident] = kis_stock(ident, token, key, sec)
            d = kr_cache[ident]
        else:
            d = None
        return std_stock_table(name, ident, d)
    return re.sub(pat, repl, body)


# ----------------------------- Claude API 작성 -----------------------------
ANTHROPIC_MODEL = "claude-sonnet-4-6"

AI_CLASSES = (
    "사용 가능한 HTML 클래스(이 클래스들만 사용):\n"
    " - <p class=\"lead-para\">…</p> : 첫 리드 문단\n"
    " - <section class=\"sec\"><div class=\"eyebrow\">SECTION</div><h2>제목</h2></section> : 섹션 헤더(섹션마다 본문 <p>가 뒤따름)\n"
    " - <div class=\"src-cat\">소제목</div> : 표/블록 위 작은 소제목\n"
    " - <table class=\"grid\"><thead><tr><th>…</th></tr></thead><tbody><tr><td>…</td></tr></tbody></table> : 데이터 표\n"
    " - 등락은 <span class=\"up\">+1.2%</span> / <span class=\"down\">-0.8%</span> (한국식: 상승=빨강 up, 하락=파랑 down)\n"
    " - <span class=\"key\">핵심 구절</span> : 꼭 읽혀야 할 핵심 키워드·사건명에 골드 밑줄 강조. 한 문단에 1~2개까지만(남발 금지).\n"
    " - <span class=\"num\">8,203.84</span> : 본문 속 중요한 '절대 수치'(지수 레벨·금액·종목수 등)를 세리프로 환기. 등락률(%)에는 쓰지 말 것 — 그건 up/down.\n"
    " - <p class=\"takeaway\">섹션 핵심 한 줄</p> : 섹션마다 가장 중요한 결론 1문장을 콜아웃 박스로(섹션당 최대 1개, 보통 섹션 끝에).\n"
    " - <strong>…</strong> : 부차(약) 강조. key/num에 해당 안 되는 일반 강조용.\n"
    " - <p class=\"small\">…</p> : 작은 보조설명·출처\n"
    " - <hr class=\"rule\"> : 구분선\n"
    " - 출처 링크: <a href=\"URL\" target=\"_blank\" rel=\"noopener\">매체명</a>\n"
    "\n[강조 규칙 — 색=역할 1:1 고정 · 강조는 '필수']\n"
    " · 본문에 강조가 하나도 없으면 안 됩니다. 평문만 늘어놓지 말고 아래 4종을 기사 전체에 골고루 반드시 적용하세요.\n"
    " · 빨강(up)·파랑(down)은 오직 '일간 등락률 수치'에만 — 본문에 등장하는 모든 등락률(%)은 빠짐없이 .up/.down으로 감쌀 것(필수).\n"
    " · .key(골드밑줄): 각 섹션 본문에서 핵심 사건명·키워드를 반드시 표시(문단당 1~2개까지, 그 이상 남발은 금지).\n"
    " · .num(세리프): 지수 레벨·중요 금액·종목수 같은 핵심 '절대수치'가 본문에 나오면 반드시 .num으로 환기(등락률(%)엔 쓰지 말 것 — 그건 up/down).\n"
    " · .takeaway: 각 섹션은 가장 중요한 결론 1문장을 .takeaway 콜아웃으로 마무리(섹션당 1개 권장, 최대 1개).\n"
    " · 원칙은 '평문이 기본, 강조는 포인트'지만 포인트가 0개여서는 안 됩니다 — 색이 흔해지지 않게 절제하되, 각 종류가 최소 1회 이상은 쓰여야 합니다.\n"
)


def _ai_user_prompt(mode, date_kst, items, asof, vol, rank_up, rank_dn, flows):
    d = date_kst
    dstr = f'{d.year}년 {d.month}월 {d.day}일({WEEKDAY_KR[d.weekday()]})'
    lines = [f'[확정 데이터 — 아래 숫자는 그대로 사용, 추가 사실은 web_search로 확인]',
             f'작성시각(KST): {asof or dstr}', f'발행일: {dstr}', f'모드: {mode}']
    idx = []
    for nm in ["KOSPI", "KOSDAQ", "USD/KRW", "WTI", "S&P 500", "나스닥", "다우존스", "달러인덱스"]:
        it = items.get(nm)
        if it:
            idx.append(f'{nm} {it.get("value","")}({it.get("change","")})')
    if idx:
        lines.append("지수/지표: " + ", ".join(idx))
    if vol:
        lines.append("거래량 상위(한국): " + "; ".join(
            f'{x["name"]}({x.get("code","")}) 현재가 {x["price"]} 등락 {x["ctrt"]}% 거래량 {x["vol"]}' for x in vol))
    if rank_up:
        lines.append("등락률 상위: " + ", ".join(f'{x["name"]}({x.get("code","")}) {x["ctrt"]}%' for x in rank_up))
    if rank_dn:
        lines.append("등락률 하위: " + ", ".join(f'{x["name"]}({x.get("code","")}) {x["ctrt"]}%' for x in rank_dn))
    if flows:
        lines.append("수급(외국인/기관/개인 순매수): " + "; ".join(
            f'{m} 외 {v.get("frgn")} 기 {v.get("orgn")} 개 {v.get("indv")}' for m, v in flows.items()))

    if mode == "close":
        focus = (
            "이번은 '한국 증시 마감 시황'입니다. 한국 뉴스 위주로 구성하되, 밤사이/장중 한국 증시에 "
            "영향을 준 해외(특히 미국) 이슈가 있으면 반드시 포함하세요. 거래량 상위 5종목(한국)은 각각 "
            "오늘 주가 흐름 + 관련 뉴스 + 수급(외국인·기관)을 엮어 분석하고, 근거를 댄 향후 주가 시나리오를 제시하세요.")
        sections = ("마감 요약(lead-para) → 지수 마감표(table.grid) → 오늘의 주요 뉴스(출처 명시) → "
                    "거래량 상위 5종목 분석(종목별 흐름·뉴스·수급·향후 시나리오) → 외국인·기관 수급 분석 → "
                    "내일 관전 포인트·전망(근거 명시)")
    else:
        focus = (
            "이번은 '개장 브리핑'입니다. 간밤 미국 증시·주요 미국 뉴스 위주로 구성하되, 한국 장에 큰 영향을 "
            "줄 이슈가 있으면 포함하세요. 미국 거래량/주목 상위 5종목을 web_search로 확인해 흐름·뉴스를 분석하고 "
            "전망을 제시하세요. 한국 코스피·코스닥은 전 거래일 종가 기준으로 출발 환경을 짚어주세요. "
            "간밤 미국 지수표에는 S&P500·나스닥·다우존스를 모두 넣되, 종가(레벨) 칸을 비우거나 '—'로 두지 말고 "
            "확정 데이터에 없으면 web_search로 확인한 마감 지수를 채우세요.")
        sections = ("개장 요약(lead-para) → 간밤 미국 지수·지표표(table.grid) → 간밤 주요 뉴스(출처 명시) → "
                    "미국 주목 5종목 분석 → 오늘 한국 증시 관전 포인트·전망(근거 명시)")

    body = "\n".join(lines)
    return (
        f"{body}\n\n"
        f"[작성 지침]\n{focus}\n\n"
        f"구성 순서: {sections}\n\n"
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
        "8) 쉬운 풀이: 중학생이 모를 만한 경제·금융 용어는 처음 나올 때 괄호로 짧게 뜻을 병기하세요(예: 서킷브레이커(주가가 급락하면 거래를 잠시 멈추는 제도), 밸류에이션(기업가치 대비 주가 수준)). 한 용어당 한 번만, 간결하게.\n"
        "9) 미국 지수·지표·기관 알파벳 병기: 처음 나올 때 알파벳/약어를 괄호로 함께 적습니다(예: 나스닥(NASDAQ), 연준(Fed), 연방공개시장위원회(FOMC), 소비자물가(CPI), 점도표(dot plot)).\n"
        "10) 실적 발표·경제지표 등 '시각이 정해진' 일정을 언급할 때는 항상 한국시간(KST)으로 환산해 원래 시간과 함께 병기합니다(예: '미 동부 오후 4시 30분(한국시간 익일 새벽 5시 30분)'). 미국은 서머타임 적용 여부에 따라 한국과의 시차가 13/14시간으로 달라지니, web_search로 해당 일정의 정확한 KST를 확인해 적습니다. 분 단위가 공시되지 않은 '장 마감 후(after market close)' 같은 표현은 그대로 옮기되, 콘퍼런스콜 등 구체 시각이 있으면 그 시각을 KST로 병기합니다.\n"
        "11) '역사적'이라는 표현은 해당 지표가 역사상 5위 이내임이 확인될 때만 사용하세요. 그 외에는 쓰지 말고 사실대로 순화합니다. '역대급'·'최근 수년간 손에 꼽히는'·'사상 최대' 같은 희소성·최상급 표현도 카운트나 공식 자료로 검증된 경우에만 쓰고, 아니면 쓰지 마세요. '어닝 쇼크'는 실적이 기대를 밑돈 경우에만 쓰고, 호실적 서프라이즈에는 쓰지 마세요(정반대 의미).\n"
        "12) 지수의 '급등/급락/약세/강세 출발(개장 기사)' 또는 '마감(마감 기사)'은 반드시 '금일 시초가' 또는 '금일 종가'의 실제 부호로만 판단해 적습니다. '전일 종가'의 등락률을 오늘의 개장/마감 등락률로 절대 재사용하지 마세요(예: 전일 종가 +5.42%를 '오늘 +5.42% 급등 출발'로 쓰면 안 됨). 표와 본문에서 '전일 종가'와 '금일 시초가/종가'를 항상 구분해 명시하고, 금일 시초가 데이터가 없으면 방향(급등/급락 등)을 단정하지 마세요.\n\n"
        f"{AI_CLASSES}\n"
        "[발행 전 자가점검] 출력하기 전에 본문을 스스로 점검하세요: ⑴ 모든 등락률(%)이 .up/.down으로 감싸졌는가, ⑵ 핵심 키워드·사건명에 .key가 (기사 전체 3개 이상) 들어갔는가, ⑶ 중요한 절대수치(지수레벨·금액)에 .num을 썼는가, ⑷ 각 섹션이 .takeaway로 마무리됐는가, ⑸ '역사적·역대급·사상 최대' 등 미검증 최상급을 쓰지 않았는가(규칙 11), ⑹ '급등/약세 출발' 등 방향을 '전일 종가'가 아닌 '금일 시초가/종가'의 실제 부호로 적었는가(규칙 12). 하나라도 어긋나면 고쳐 다시 작성한 뒤 출력하세요.\n\n"
        "[출력 형식] 아래 형식 '그대로' 출력하세요. 각 구분선(===...===)을 정확히 쓰고 그 사이에 내용만 넣으세요. "
        "마크다운 코드펜스(```)나 형식 밖의 다른 말은 절대 쓰지 마세요. body_html은 위 클래스만 쓴 순수 HTML입니다.\n"
        "===TITLE===\n"
        "(기사 제목 20~45자, 핵심 수치 포함)\n"
        "===SUBTITLE===\n"
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
            with urllib.request.urlopen(req, timeout=240) as r:
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

    title = sect("===TITLE===", "===SUBTITLE===")
    subtitle = sect("===SUBTITLE===", "===SUMMARY===")
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


def compose_with_claude(api_key, mode, date_kst, items, asof, vol, rank_up, rank_dn, flows):
    system = (
        "당신은 한국의 데일리 투자 뉴스레터 '투자이야기(INVEST STORY)'의 증시 전문 기자입니다. "
        "기사는 '박철웅 기자' 명의로 공개 발행됩니다. 정확성과 출처 표기를 최우선으로 하며, 확인되지 않은 "
        "사실이나 과장된 추측은 쓰지 않습니다. 제공된 확정 수치는 그대로 쓰고, 그 외 사실·뉴스·종목 동향은 "
        "web_search로 직접 확인해 출처를 답니다. 한국 증시 색상 관례(상승=빨강, 하락=파랑)를 따릅니다. "
        "반드시 지정된 구분자 형식으로만 출력합니다."
    )
    user = _ai_user_prompt(mode, date_kst, items, asof, vol, rank_up, rank_dn, flows)
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
 /* ===== 본문 강조 키트(색=역할 1:1) · 2026-06-23 추가 ===== */
 main .key{font-weight:700;color:var(--ink);background:linear-gradient(transparent 60%,rgba(201,166,84,.45) 60%);padding:0 1px;border-radius:1px}
 main .num{font-family:var(--serif);font-weight:700;color:var(--navy);font-variant-numeric:tabular-nums}
 main strong{font-weight:700;color:var(--ink);letter-spacing:-.01em}
 p.takeaway{margin:16px 0 6px;padding:12px 15px;background:#FBF7EE;border-left:4px solid var(--gold);border-radius:0 7px 7px 0;font-family:var(--serif);font-weight:700;color:var(--navy);font-size:15px;line-height:1.62}
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
 /* ===== 기사 요약 라벨(시안 C) + 모바일 표 호환 · 2026-06-22 추가 ===== */
 .art-hero + .lead-para{font-size:16px;line-height:1.78;border:1px solid #E2E6EC;border-radius:4px;background:#fff;padding:15px 18px 16px;margin:18px 0 24px;position:relative}
 .art-hero + .lead-para::before{content:"기사 요약";display:block;background:#1B3C6E;color:#fff;font-weight:700;font-size:12px;letter-spacing:.12em;padding:9px 18px;margin:-15px -18px 13px;border-left:4px solid #C9A654;border-radius:4px 4px 0 0}
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


def enrich_highlights(body_html):
    """강조 누락 안전망: 본문 산문에 들어간 '맨몸 등락률(±%)'을 자동으로 .up/.down 으로 감싼다.
    - 표(<table>…</table>)와 기존 <span>…</span> 안은 절대 건드리지 않는다(중복 래핑·표 훼손 방지).
    - 부호 없는 값(기준금리·물가율·지수 레벨 등)은 손대지 않는다(부호 있는 일간 등락률만 대상)."""
    if not body_html:
        return body_html
    protect = re.compile(r'(<table\b.*?</table>|<span\b.*?</span>)', re.S | re.I)
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
    title = str(meta["title"]).strip()
    subtitle = str(meta.get("subtitle", "")).strip()
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
                '<meta name="twitter:card" content="summary">\n')
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
        vol = fetch_volume_rank(token, key, sec, 3)
        print("거래량 상위:", "OK " + ", ".join(x["name"] for x in vol) if vol else "실패(섹션 생략됨)")
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

    if mode == "open":
        dji = fmp_index("^DJI", os.environ.get("FMP_API_KEY", "").strip())
        if dji:
            items["다우존스"] = dji
            print(f"[daily_news] 다우존스 종가 확보: {dji['value']}({dji['change']})")
        else:
            print("[daily_news] 다우존스 종가 미확보(FMP) — AI가 web_search로 채움")

    rank_up = rank_dn = flows = vol = None
    if token:
        if mode == "close":
            rank_up = fetch_fluctuation(token, key, sec, "0", 5)
            rank_dn = fetch_fluctuation(token, key, sec, "1", 5)
            vol = fetch_volume_rank(token, key, sec, 5)
            flows = fetch_investors(token, key, sec)
            print(f"[daily_news] 거래량상위={'O' if vol else 'X'} 등락상위={'O' if rank_up else 'X'} "
                  f"등락하위={'O' if rank_dn else 'X'} 수급={'O' if flows else 'X'}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    htmlstr = title = summary = None
    if api_key:
        try:
            meta = compose_with_claude(api_key, mode, now, items, asof, vol, rank_up, rank_dn, flows)
            try:
                meta["body_html"] = inject_stock_tables(meta["body_html"], token, key, sec)
            except Exception as e:
                sys.stderr.write(f"[stock] 종목표 삽입 경고: {e}\n")
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
