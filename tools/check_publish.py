# -*- coding: utf-8 -*-
"""check_publish.py — 발행 누락 감시기.

나왔어야 할 기사가 라이브 manifest에 등록돼 있는지 확인하고, 없으면 워크플로를
실패(exit 1)시켜 GitHub 알림 메일이 가게 한다. 아무것도 커밋하지 않는다.

판정 기준(KST): 주말 제외 · 09:30 이후 개장호 · 16:30 이후 마감호.
휴장일은 KIS로 판정하고, 토큰이 없으면 '개장호가 있으면 거래일'로 추정한다.

실행:  python tools/check_publish.py [--at 16:30]
"""
import json, os, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifest.json")
KST = timezone(timedelta(hours=9))
OPEN_DUE_MIN = 9 * 60 + 30
CLOSE_DUE_MIN = 16 * 60 + 30


def is_trading_day(now):
    if now.weekday() >= 5:
        return False, "주말"
    key = os.environ.get("KIS_APP_KEY", "").strip()
    sec = os.environ.get("KIS_APP_SECRET", "").strip()
    if key and sec:
        try:
            import daily_news as dn
            token = dn.kis_auth.get_token(key, sec, allow_issue=False)
            if token:
                ok, why = dn.trading_day(now, token, key, sec)
                return ok, f"KIS 판정({why})"
        except Exception as e:
            sys.stderr.write(f"::warning::KIS 휴장일 판정 실패: {e}\n")
    return None, "판정 근거 없음(KIS 토큰 미보유)"


def main():
    now = datetime.now(KST)
    for i, a in enumerate(sys.argv):
        if a == "--at" and i + 1 < len(sys.argv):
            hh, mm = sys.argv[i + 1].split(":")
            now = now.replace(hour=int(hh), minute=int(mm))
    day = now.strftime("%Y-%m-%d")
    minutes = now.hour * 60 + now.minute
    print(f"[check_publish] 기준 시각 {now:%Y-%m-%d(%a) %H:%M} KST")

    trading, why = is_trading_day(now)
    if trading is False:
        print(f"[check_publish] {why} — 검사 대상 아님")
        return 0

    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    files = {i.get("file", "") for i in man.get("issues", [])}
    have_open = f"newsletters/{day}-open.html" in files
    have_close = f"newsletters/{day}-close.html" in files

    if trading is None:
        if not have_open and minutes >= OPEN_DUE_MIN:
            print(f"[check_publish] {why} · 개장호도 없음 — 휴장일 가능성이 있어 경고만 남기고 통과")
            sys.stderr.write(f"::warning::{day} 개장호가 없습니다. 휴장일이 아니라면 발행 실패입니다\n")
            return 0
        print(f"[check_publish] {why} · 개장호 존재 → 거래일로 간주")

    missing = []
    if minutes >= OPEN_DUE_MIN and not have_open:
        missing.append(f"개장호 newsletters/{day}-open.html (08:50 예정)")
    if minutes >= CLOSE_DUE_MIN and not have_close:
        missing.append(f"마감호 newsletters/{day}-close.html (15:35 예정)")

    print(f"[check_publish] {day} 개장호 {'O' if have_open else 'X'} · 마감호 {'O' if have_close else 'X'} "
          f"· manifest 총 {len(man.get('issues', []))}건")
    if not missing:
        print("[check_publish] 이상 없음")
        return 0
    for m in missing:
        sys.stderr.write(f"::error::발행 누락: {m}\n")
    sys.stderr.write(
        "::error::daily-news 최근 실행 로그의 '데일리 기사 생성' 단계를 확인하세요. "
        "exit 2는 ①AI 작성 실패(ANTHROPIC 잔액/인증) ②지수 검산 ③날짜-요일 검산 중 하나입니다.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
