# -*- coding: utf-8 -*-
"""update_ticker.py — INVEST STORY 배너 시세 자동 갱신기.

지수/환율을 받아 ticker.json을 갱신한다. 우선순위(2026-07-08 재배열):
  1) KIS(국내지수 실시간) → 2) 야후(무키·무제한) → 3) Twelve Data(야후 실패분만 lazy 보강)
Twelve Data가 심볼 미지원·플랜 제한으로 축소되고 야후가 실측 신뢰도를 입증해 순서를 교체.
한 종목이 실패하면 기존 ticker.json 값을 유지한다(절대 비우지 않음).

로컬/액션 실행:  python tools/update_ticker.py
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
import kis_auth   # KIS 토큰 공용 캐시(읽기 전용으로 사용)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKER = os.path.join(ROOT, "ticker.json")
KST = timezone(timedelta(hours=9))

# (표시이름, 야후심볼(URL인코딩), 트웰브데이터심볼, 표시방식)  fmt: "idx"=지수%, "fx"=환율 절대값, "oil"=$가격+%, "dxy"=달러인덱스
SYMBOLS = [
    ("KOSPI",     "%5EKS11",   "KS11",     "idx"),
    ("KOSDAQ",    "%5EKQ11",   "KQ11",     "idx"),
    ("USD/KRW",   "KRW=X",     "USD/KRW",  "fx"),
    ("WTI",       "CL=F",      "WTI/USD",  "oil"),
    ("S&P 500",   "%5EGSPC",   "GSPC",     "idx"),
    ("나스닥",     "%5EIXIC",   "IXIC",     "idx"),
    ("달러인덱스",  "DX-Y.NYB",  "DXY",      "dxy"),
    # 2026-08-24 추가 — 금 2종.
    #  · KRX 금: 한국거래소 금시장 '금 99.99_1Kg'(종목코드 04020000) 원/g.
    #    야후·TD에 없는 국내 전용 데이터라 야후 심볼은 None이고 fetch_krx_gold()가 전담한다.
    #  · 국제 금: 런던 현물 XAU/USD($/트로이온스). 선물(GC=F)이 아니라 '현물' 기준.
    ("KRX 금",     None,        None,       "krxgold"),
    ("국제 금",     "XAUUSD=X",  "XAU/USD",  "goldusd"),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_quote(symbol):
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last_err = None
    for host in hosts:
        for attempt in range(2):  # 호스트당 2회 재시도
            try:
                url = ("https://" + host + "/v8/finance/chart/"
                       + symbol + "?range=1d&interval=1d")
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA, "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.load(r)
                meta = data["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose", meta.get("previousClose"))
                if price is None or prev is None:
                    raise ValueError("missing price/prev")
                return float(price), float(prev)
            except Exception as e:
                last_err = e
                time.sleep(0.8 * (attempt + 1))
    raise last_err if last_err else RuntimeError("fetch failed")

def comma(x, dp):
    return f"{x:,.{dp}f}"

def make_item(name, fmt, price, prev):
    diff = price - prev
    pct = (diff / prev * 100.0) if prev else 0.0
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    sign = "+" if diff > 0 else ("" if diff == 0 else "-")
    if fmt == "fx":   # 환율: 절대값 변화(원)
        value = comma(price, 1)
        change = f"{sign}{abs(diff):.1f}"
    elif fmt == "oil": # 유가: $가격 + %
        value = f"${price:,.2f}"
        change = f"{sign}{abs(pct):.2f}%"
    elif fmt == "dxy":
        value = comma(price, 2)
        change = f"{sign}{abs(pct):.2f}%"
    elif fmt == "krxgold":   # KRX 금현물: 원/g 정수 + %
        value = comma(price, 0)
        change = f"{sign}{abs(pct):.2f}%"
    elif fmt == "goldusd":   # 국제 금 현물: $/트로이온스 + %
        value = f"${price:,.2f}"
        change = f"{sign}{abs(pct):.2f}%"
    else:             # 지수: %
        value = comma(price, 2)
        change = f"{sign}{abs(pct):.2f}%"
    return {"name": name, "value": value, "change": change, "dir": direction}

def fetch_td_all(key):
    """Twelve Data /quote 일괄조회. {td_symbol: (close, prev)} 반환."""
    syms = ",".join(td for _, _, td, _ in SYMBOLS if td)
    url = ("https://api.twelvedata.com/quote?symbol="
           + urllib.parse.quote(syms, safe=",/") + "&apikey=" + urllib.parse.quote(key))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    out = {}
    if not isinstance(data, dict):
        return out
    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "twelvedata error"))
    # 다종목이면 {심볼: quote}, 단일이면 quote 자체
    iterable = data.items() if any(isinstance(v, dict) for v in data.values()) else [(SYMBOLS[0][2], data)]
    for td, q in iterable:
        if not isinstance(q, dict) or q.get("status") == "error":
            sys.stderr.write(f"[td] {td} 건너뜀: {q.get('message') if isinstance(q,dict) else q}\n")
            continue
        def num(k):
            v = q.get(k)
            try:
                return float(v) if v not in (None, "") else None
            except Exception:
                return None
        close = num("close")
        prev = num("previous_close")
        if close is None:
            sys.stderr.write(f"[td] {td} close 없음\n"); continue
        if prev is None:                    # previous_close가 없으면 change/percent로 역산
            chg = num("change"); pct = num("percent_change")
            if chg is not None:
                prev = close - chg
            elif pct is not None and pct != -100:
                prev = close / (1 + pct / 100.0)
            else:
                prev = close       # 최후수단: 변동 0 처리(값은 표시)
        out[td] = (close, prev)
    return out

KIS_BASE = "https://openapi.koreainvestment.com:9443"

def kis_token(app_key, app_secret):
    """KIS 접근토큰 발급(24h 유효)."""
    body = json.dumps({"grant_type": "client_credentials",
                       "appkey": app_key, "appsecret": app_secret}).encode()
    req = urllib.request.Request(KIS_BASE + "/oauth2/tokenP", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]

def kis_index(token, app_key, app_secret, iscd):
    """국내 업종 현재지수 조회 → (현재지수, 전일종가)."""
    url = (KIS_BASE + "/uapi/domestic-stock/v1/quotations/inquire-index-price"
           "?FID_COND_MRKT_DIV_CODE=U&FID_INPUT_ISCD=" + iscd)
    req = urllib.request.Request(url, headers={
        "Content-Type": "application/json",
        "authorization": "Bearer " + token,
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": "FHPUP02100000", "custtype": "P",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1", "kis error"))
    o = data["output"]
    price = float(o["bstp_nmix_prpr"])             # 현재지수
    vrss = float(o["bstp_nmix_prdy_vrss"])         # 전일대비(부호 포함)
    return price, price - vrss                      # (현재, 전일종가)

def fetch_kis(app_key, app_secret):
    """KIS로 KOSPI·KOSDAQ 조회 → {표시이름: (현재, 전일종가)}.
    토큰은 공용 캐시에서만 가져온다(여기서는 발급하지 않음). 유효 토큰이 없으면
    KIS 호출을 건너뛰고 {}를 돌려줘 직전 ticker.json 값이 유지되게 한다.
    (실제 발급은 매일 개장 브리핑(08:50 KST) 때 daily_news.py만 수행 → 한투 '1일 1회' 준수.)"""
    token = kis_auth.get_token(app_key, app_secret, allow_issue=False)
    if not token:
        sys.stderr.write("[kis] 유효 토큰 없음 — KIS 호출 생략(직전 값 유지)\n")
        return {}
    out = {}
    for name, iscd in (("KOSPI", "0001"), ("KOSDAQ", "1001")):
        try:
            price, prev = kis_index(token, app_key, app_secret, iscd)
            # 사고#32 대응: 국내 증시 개장(09:00) 전에는 KIS가
            #   현재지수 = 전일 종가, 전일대비 = 0 을 돌려준다.
            # 이 값을 그대로 쓰면 '0.00% 보합'으로 박제되므로(08:50 개장호 빌드에서 발생),
            # 야후 폴백에 위임한다. 야후는 chartPreviousClose를 주므로
            # '전일 종가 + 그날의 실제 등락률'이 정상 표기된다.
            if price == prev:
                sys.stderr.write(
                    f"[kis] {name} 전일대비 0 (개장 전 추정) — 야후 폴백에 위임\n")
                continue
            out[name] = (price, prev)
        except Exception as e:
            sys.stderr.write(f"[kis] {name} 실패: {e}\n")
    return out

# ----------------------------- 금시세(2026-08-24 추가) -----------------------------
# KRX 금은 야후·Twelve Data 어디에도 없어 전용 수집 경로가 필요하다.
# 정책: 다중 폴백 + 절대 비우지 않기(전 경로 실패 시 예외 → main이 직전 ticker.json 값 유지).
# 어느 경로가 살아있는지는 tools/check_gold.py 로 언제든 점검할 수 있다.

KRX_ISU = "04020000"          # 금 99.99_1Kg (KRX 금시장 대표 종목)
KRX_ISU_NAME = "금 99.99_1Kg"


def _http(url, data=None, headers=None, timeout=15):
    hd = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
          "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if headers:
        hd.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hd.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req = urllib.request.Request(url, data=body, headers=hd,
                                 method=("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _f(x):
    """'147,230' · '-1.23' · 147230 → float. 실패하면 None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().replace(",", "").replace("%", "").replace("원", "")
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except Exception:
        return None


def _pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def krx_gold_from_krx():
    """1순위 — KRX 정보데이터시스템 [금시장 일별매매정보].
    당일 데이터가 아직 없으면(개장 전·휴장) 최대 7영업일 거슬러 올라간다.
    반환: (현재가, 전일종가) 원/g."""
    url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    ref = ("https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"
           "?menuId=MDC0201040603")
    today = datetime.now(KST).date()
    for back in range(0, 8):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:          # 주말은 건너뜀
            continue
        try:
            raw = _http(url, data={
                "bld": "dbms/MDC/STAT/standard/MDCSTAT15001",
                "trdDd": d.strftime("%Y%m%d"),
                "share": "1", "money": "1", "csvxls_isNo": "false",
            }, headers={"Referer": ref})
            js = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"KRX 응답 실패: {e}")
        rows = js.get("OutBlock_1") or js.get("output") or []
        for row in rows:
            code = str(row.get("ISU_CD", "")).strip()
            name = str(row.get("ISU_NM", "")).strip()
            if code != KRX_ISU and KRX_ISU_NAME not in name:
                continue
            close = _f(_pick(row, "TDD_CLSPRC", "CLSPRC", "TDD_CLSPRC_PRC"))
            diff = _f(_pick(row, "CMPPREVDD_PRC"))
            if close is None:
                continue
            if diff is None:
                rt = _f(_pick(row, "FLUC_RT"))
                if rt is None or rt == -100:
                    raise RuntimeError("KRX 전일대비 없음")
                prev = close / (1 + rt / 100.0)
            else:
                prev = close - diff
            print(f"[gold] KRX 정보데이터시스템 수신 · {d} {name} {close:,.0f}원/g")
            return close, prev
    raise RuntimeError("KRX 금시장 데이터에서 대상 종목을 찾지 못함(최근 7영업일)")


def krx_gold_from_naver():
    """2순위 — 네이버 금융 국내 금시세(M04020000). 응답 스키마 변동에 대비해 방어적으로 파싱."""
    last = None
    for url in ("https://api.stock.naver.com/marketindex/metals/M04020000",
                "https://m.stock.naver.com/api/marketindex/metals/M04020000"):
        try:
            js = json.loads(_http(url, headers={"Referer": "https://m.stock.naver.com/"}))
        except Exception as e:
            last = e
            continue
        d = js.get("result", js) if isinstance(js, dict) else js
        if isinstance(d, list) and d:
            d = d[0]
        if not isinstance(d, dict):
            continue
        close = _f(_pick(d, "closePrice", "nowVal", "currentPrice", "tradePrice", "price"))
        if close is None:
            continue
        pct = _f(_pick(d, "fluctuationsRatio", "changeRate", "fluctuationRate"))
        if pct is not None and pct != -100:
            prev = close / (1 + pct / 100.0)
        else:
            diff = _f(_pick(d, "compareToPreviousClosePrice", "changeVal", "change"))
            if diff is None:
                continue
            # 네이버는 등락 부호를 별도 코드로 준다(2·1=상승, 5·4=하락). 없으면 상승으로 간주.
            sgn = _pick(d, "compareToPreviousPrice")
            code = str(sgn.get("code", "")) if isinstance(sgn, dict) else str(sgn or "")
            if code in ("4", "5"):
                diff = -abs(diff)
            prev = close - diff
        print(f"[gold] 네이버 금융 수신 · {close:,.0f}원/g")
        return close, prev
    raise RuntimeError(f"네이버 국내 금시세 파싱 실패: {last}")


def fetch_krx_gold():
    """KRX 금(원/g) — 정보데이터시스템 → 네이버 순으로 시도. 전부 실패하면 예외."""
    errs = []
    for fn in (krx_gold_from_krx, krx_gold_from_naver):
        try:
            price, prev = fn()
            if price and prev and price > 0 and prev > 0:
                return price, prev
            errs.append(f"{fn.__name__}: 값 이상({price},{prev})")
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    raise RuntimeError(" / ".join(errs))


def load_prev_ticker():
    try:
        with open(TICKER, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"asof": "", "items": []}

def main():
    prev_tk = load_prev_ticker()
    prev_by_name = {it["name"]: it for it in prev_tk.get("items", [])}
    key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    kis_key = os.environ.get("KIS_APP_KEY", "").strip()
    kis_secret = os.environ.get("KIS_APP_SECRET", "").strip()

    kis_data = {}
    if kis_key and kis_secret:
        try:
            kis_data = fetch_kis(kis_key, kis_secret)
            print(f"[update_ticker] KIS {len(kis_data)}/2 수신 (KOSPI·KOSDAQ)")
        except Exception as e:
            sys.stderr.write(f"[warn] KIS 실패: {e}\n")

    # 1·2순위 수집: KIS → 야후. 실패분은 pending에 모아 TD로 lazy 보강.
    quotes, pending, yh_ok, gold_src = {}, [], 0, ""
    yh_total = sum(1 for _n, _s, _t, _f in SYMBOLS if _s)
    for name, sym, td, fmt in SYMBOLS:
        if name in kis_data:
            quotes[name] = kis_data[name]
            continue
        if fmt == "krxgold":
            # KRX 금은 야후·TD에 없다 → 전담 폴백 체인. 실패해도 다른 종목 발행을 막지 않는다.
            try:
                quotes[name] = fetch_krx_gold()
                gold_src = "KRX금"
            except Exception as e:
                sys.stderr.write(f"[warn] KRX 금 전 경로 실패: {e} — 직전값 유지\n")
            continue
        if not sym:
            continue
        try:
            quotes[name] = fetch_quote(sym)
            yh_ok += 1
            time.sleep(0.4)  # 야후 과호출 방지
        except Exception as e:
            sys.stderr.write(f"[warn] {name} ({sym}) 야후 실패: {e}\n")
            if td:
                pending.append((name, td))
    if yh_ok:
        print(f"[update_ticker] 야후 {yh_ok}/{yh_total} 수신")

    # 3순위: 야후가 못 채운 종목만 Twelve Data로 보강(크레딧 절약형 lazy 호출)
    td_used = 0
    if pending and key:
        try:
            td_data = fetch_td_all(key)
            for name, td in pending:
                if td in td_data:
                    quotes[name] = td_data[td]
                    td_used += 1
            print(f"[update_ticker] Twelve Data 보강 {td_used}/{len(pending)}종목")
        except Exception as e:
            sys.stderr.write(f"[warn] Twelve Data 실패: {e} — 기존값 유지\n")

    items, ok = [], 0
    for name, sym, td, fmt in SYMBOLS:
        if name in quotes:
            price, prev = quotes[name]
            items.append(make_item(name, fmt, price, prev))
            ok += 1
        elif name in prev_by_name:
            items.append(prev_by_name[name])
        else:
            items.append({"name": name, "value": "—", "change": "확인 중", "dir": "flat"})

    if ok == 0:
        sys.stderr.write("[error] 모든 종목 실패 — ticker.json 변경 안 함\n")
        return 1
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    parts = []
    if kis_data:
        parts.append("KIS")
    if yh_ok:
        parts.append("야후")
    if td_used:
        parts.append("Twelve Data")
    if gold_src:
        parts.append(gold_src)
    src_label = "+".join(parts) if parts else "직전값"
    out = {"asof": now + f" (자동·{src_label})", "items": items}
    with open(TICKER, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[update_ticker] {ok}/{len(SYMBOLS)} 갱신 · asof {now} · src {src_label}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
