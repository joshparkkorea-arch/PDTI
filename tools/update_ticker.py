# -*- coding: utf-8 -*-
"""update_ticker.py — INVEST STORY 배너 시세 자동 갱신기.

야후 파이낸스 공개 차트 엔드포인트에서 지수/환율을 받아 ticker.json을 갱신한다.
키가 필요 없고 서버(깃허브 액션)에서 실행하면 CORS 문제도 없다.
한 종목이 실패하면 기존 ticker.json 값을 유지한다(절대 비우지 않음).

로컬/액션 실행:  python tools/update_ticker.py
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKER = os.path.join(ROOT, "ticker.json")
KST = timezone(timedelta(hours=9))

# (표시이름, 야후심볼, 표시방식)  fmt: "idx"=지수%, "fx"=환율 절대값, "dxy"=달러인덱스
SYMBOLS = [
    ("KOSPI",     "%5EKS11",   "idx"),
    ("KOSDAQ",    "%5EKQ11",   "idx"),
    ("USD/KRW",   "KRW=X",     "fx"),
    ("WTI",       "CL=F",      "oil"),
    ("S&P 500",   "%5EGSPC",   "idx"),
    ("나스닥",     "%5EIXIC",   "idx"),
    ("달러인덱스",  "DX-Y.NYB",  "dxy"),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_quote(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + symbol + "?range=1d&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    meta = data["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose", meta.get("previousClose"))
    if price is None or prev is None:
        raise ValueError("missing price/prev")
    return float(price), float(prev)

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

def load_prev_ticker():
    try:
        with open(TICKER, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"asof": "", "items": []}

def main():
    prev_tk = load_prev_ticker()
    prev_by_name = {it["name"]: it for it in prev_tk.get("items", [])}
    items, ok = [], 0
    for name, sym, fmt in SYMBOLS:
        try:
            price, prev = fetch_quote(sym)
            items.append(make_item(name, fmt, price, prev))
            ok += 1
            time.sleep(0.4)  # 야후 과호출 방지
        except Exception as e:
            sys.stderr.write(f"[warn] {name} ({sym}) 실패: {e} — 기존값 유지\n")
            if name in prev_by_name:
                items.append(prev_by_name[name])
            else:
                items.append({"name": name, "value": "—", "change": "확인 중", "dir": "flat"})
    if ok == 0:
        sys.stderr.write("[error] 모든 종목 실패 — ticker.json 변경 안 함\n")
        return 1
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    out = {"asof": now + " (자동)", "items": items}
    with open(TICKER, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[update_ticker] {ok}/{len(SYMBOLS)} 갱신 · asof {now}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
