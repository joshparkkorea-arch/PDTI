# -*- coding: utf-8 -*-
"""check_gold.py — 금시세 수집 경로 자가진단 (2026-08-24 신설).

KRX 금(원/g)은 야후·Twelve Data에 없어 전용 폴백 체인으로 받는다.
이 스크립트는 그 체인의 각 소스를 순서대로 직접 두드려 '지금 어느 경로가 살아있는지'를
한눈에 보여준다. 소스가 막히면(스키마 변경·차단) 여기서 먼저 잡힌다.

실행:  python tools/check_gold.py
       (GitHub Actions → 'tools' 워크플로 → task=check-gold 로도 실행 가능)

종료코드: 0 = KRX·국제 금 모두 최소 1개 경로 생존 / 1 = 어느 한쪽이라도 전멸
"""
import os, sys, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_ticker as ut   # noqa: E402


def probe(label, fn, unit):
    """fn() → (현재가, 전일종가). 결과를 한 줄로 리포트하고 성공 여부를 돌려준다."""
    try:
        price, prev = fn()
        if not price or not prev or price <= 0 or prev <= 0:
            raise ValueError(f"값 이상 (price={price}, prev={prev})")
        pct = (price - prev) / prev * 100.0
        print(f"  ✅ {label:<34} {price:>12,.2f} {unit}  (전일 {prev:,.2f} · {pct:+.2f}%)")
        return True
    except Exception as e:
        print(f"  ❌ {label:<34} 실패: {e}")
        if os.environ.get("GOLD_DEBUG"):
            traceback.print_exc()
        return False


def main():
    print("=" * 78)
    print("INVEST STORY · 금시세 수집 경로 자가진단")
    print("=" * 78)

    print("\n[1] KRX 금 — 한국거래소 금시장 '금 99.99_1Kg' (원/그램)")
    krx = [
        probe("1순위 KRX 정보데이터시스템", ut.krx_gold_from_krx, "원/g"),
        probe("2순위 네이버 금융", ut.krx_gold_from_naver, "원/g"),
    ]

    print("\n[2] 국제 금 — 런던 현물 XAU/USD (달러/트로이온스)")
    intl = [probe("1순위 야후 XAUUSD=X", lambda: ut.fetch_quote("XAUUSD=X"), "$/oz")]
    td_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if td_key:
        def _td():
            data = ut.fetch_td_all(td_key)
            if "XAU/USD" not in data:
                raise RuntimeError("응답에 XAU/USD 없음(무료 플랜 미지원 가능)")
            return data["XAU/USD"]
        intl.append(probe("2순위 Twelve Data XAU/USD", _td, "$/oz"))
    else:
        print("  ⏭  2순위 Twelve Data XAU/USD          건너뜀 (TWELVEDATA_API_KEY 없음)")

    print("\n[참고] 아래는 폴백이 아니라 대조용입니다 — 현물과 선물은 다른 값입니다.")
    probe("(참고) 야후 GC=F COMEX 금 선물", lambda: ut.fetch_quote("GC=F"), "$/oz")

    print("\n" + "-" * 78)
    krx_ok, intl_ok = any(krx), any(intl)
    print(f"KRX 금  : {'정상 — 생존 경로 ' + str(sum(krx)) + '개' if krx_ok else '전 경로 실패 → 직전값 유지로 동작(칸이 굳습니다)'}")
    print(f"국제 금 : {'정상 — 생존 경로 ' + str(sum(intl)) + '개' if intl_ok else '전 경로 실패 → 직전값 유지로 동작(칸이 굳습니다)'}")
    if krx_ok and intl_ok:
        print("\n결과: 이상 없음. 두 지표 모두 자동 갱신됩니다.")
        return 0
    print("\n결과: 점검 필요. 실패한 소스의 응답 스키마가 바뀌었거나 차단됐을 수 있습니다.")
    print("      GOLD_DEBUG=1 로 다시 실행하면 전체 스택트레이스를 볼 수 있습니다.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
