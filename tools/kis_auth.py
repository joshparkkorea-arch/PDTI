# -*- coding: utf-8 -*-
"""kis_auth.py — KIS 접근토큰 공용 캐시.

KIS 접근토큰은 24시간 유효하고 '1일 1회 발급 원칙'이다.
.cache/kis_token.json 에 토큰+만료시각을 저장해 두고 재사용하여 하루 1회만 발급한다.
  · daily_news.py  : get_token(..., allow_issue=True)  → 없으면 발급(매일 9:05 개장 때).
  · update_ticker.py: get_token(..., allow_issue=False) → 캐시만 읽고 없으면 None(발급 안 함).
토큰은 절대 깃에 커밋하지 않는다(.gitignore 의 .cache/). 깃허브 액션에선 actions/cache로 공유한다.
"""
import json, os, time, urllib.request

KIS_BASE = "https://openapi.koreainvestment.com:9443"
ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache", "kis_token.json")
MARGIN = 600   # 만료 10분 전이면 무효로 간주(여유 두고 갱신)

def _read():
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("access_token") and float(d.get("expires_at", 0)) - time.time() > MARGIN:
            return d["access_token"]
    except Exception:
        pass
    return None

def _write(tok, expires_in):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"access_token": tok, "expires_at": time.time() + float(expires_in)}, f)
    except Exception:
        pass

def _issue(app_key, app_secret):
    body = json.dumps({"grant_type": "client_credentials",
                       "appkey": app_key, "appsecret": app_secret}).encode()
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(KIS_BASE + "/oauth2/tokenP", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            tok = data.get("access_token")
            if tok:
                _write(tok, data.get("expires_in", 86400))
                return tok
            raise RuntimeError(data.get("error_description") or data.get("msg1") or "no access_token")
        except Exception as e:
            last = e
            if attempt == 0:
                time.sleep(65)   # 1분당 1회 발급 제한 회피
    raise last

def get_token(app_key, app_secret, allow_issue=True):
    """유효한 캐시 토큰을 반환. 없을 때 allow_issue=True면 1회 발급(+캐시),
    allow_issue=False면 None(발급하지 않음 — 시세 갱신기 전용)."""
    tok = _read()
    if tok:
        return tok
    if not allow_issue:
        return None
    return _issue(app_key, app_secret)
