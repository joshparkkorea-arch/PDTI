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
    else:             # 지수: %
        value = comma(price, 2)
        change = f"{sign}{abs(pct):.2f}%"
    return {"name": name, "value": value, "change": change, "dir": direction}

def fetch_td_all(key):
    """Twelve Data /quote 일괄조회. {td_symbol: (close, prev)} 반환."""
    syms = ",".join(td for _, _, td, _ in SYMBOLS)
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
    (실제 발급은 매일 9:05 개장 때 daily_news.py만 수행 → 한투 '1일 1회' 준수.)"""
    token = kis_auth.get_token(app_key, app_secret, allow_issue=False)
    if not token:
        sys.stderr.write("[kis] 유효 토큰 없음 — KIS 호출 생략(직전 값 유지)\n")
        return {}
    out = {}
    for name, iscd in (("KOSPI", "0001"), ("KOSDAQ", "1001")):
        try:
            out[name] = kis_index(token, app_key, app_secret, iscd)
        except Exception as e:
            sys.stderr.write(f"[kis] {name} 실패: {e}\n")
    return out

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
    quotes, pending, yh_ok = {}, [], 0
    for name, sym, td, fmt in SYMBOLS:
        if name in kis_data:
            quotes[name] = kis_data[name]
            continue
        try:
            quotes[name] = fetch_quote(sym)
            yh_ok += 1
            time.sleep(0.4)  # 야후 과호출 방지
        except Exception as e:
            sys.stderr.write(f"[warn] {name} ({sym}) 야후 실패: {e}\n")
            pending.append((name, td))
    if yh_ok:
        print(f"[update_ticker] 야후 {yh_ok}/{len(SYMBOLS)} 수신")

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
    src_label = "+".join(parts) if parts else "직전값"
    out = {"asof": now + f" (자동·{src_label})", "items": items}
    with open(TICKER, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[update_ticker] {ok}/{len(SYMBOLS)} 갱신 · asof {now} · src {src_label}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
