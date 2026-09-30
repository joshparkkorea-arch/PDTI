#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""푸시 거절 후 '원격 리셋 → 산출물 재적용' 화해기 (사고#36, 2026-09-30 신설).

배경
----
워크플로는 push가 거절되면 `git rebase origin/main`으로 따라잡으려 했다. 그런데 이
리포지터리가 커밋하는 파일은 대부분 '매번 통째로 다시 쓰이는 스냅샷'이라 텍스트
3-way 병합이 성립하지 않는다. 같은 줄이 양쪽에서 바뀌므로 rebase는 반드시 충돌하고,
`git rebase --abort`는 리셋 전 상태로 되돌리기 때문에 다음 시도가 완전히 같은 충돌을
다시 만난다. 재시도 5회가 통째로 무의미해진다(2026-09-30 update-ticker 실패 로그).

해법
----
rebase를 버리고 `git reset --hard origin/main`으로 원격을 그대로 채택한 뒤, 우리
산출물을 '파일 성격에 맞는 방식으로' 다시 얹는다. 이 스크립트가 그 재적용을 맡는다.

  newsletters/*.html   신규 파일이라 충돌 자체가 없다 → 원격에 없는 것만 복사
  manifest.json        라이브가 계속 갱신하는 살아있는 파일. 원격(최신)을 기준으로 두고
                       우리 신규 항목만 append 한다. 통째로 덮어쓰지 않는다(사고#18).
  data/*.json          append-only 이력 → 키 기준 합집합, 원격 값 우선
  index.html           manifest에서 언제든 재생성 가능 → 여기서 손대지 않는다.
                       호출자가 이 스크립트 다음에 tools/build_site.py를 돌린다.

사용법
------
  python tools/reconcile_publish.py <백업디렉터리>

백업디렉터리는 push 시도 전에 워크플로가 떠 둔 산출물 사본이다
(newsletters/, manifest.json, data/).

종료 코드: 항상 0(정상). 실패는 예외로 드러낸다.
"""

import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jdump(path, obj, indent=2):
    # daily_news.py의 저장 형식과 정확히 일치시킨다(끝 개행 없음) — 불필요한 diff 방지.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)


def reconcile_manifest(backup_dir):
    """원격 manifest에 우리 신규 항목만 덧붙인다. 반환: 추가한 항목 수."""
    live_p = os.path.join(REPO, "manifest.json")
    mine_p = os.path.join(backup_dir, "manifest.json")
    live = jload(live_p)
    mine = jload(mine_p)
    if not live or not mine:
        print("[reconcile] manifest.json 한쪽이 없거나 읽히지 않음 — 건너뜀")
        return 0

    live_files = {i.get("file") for i in live.get("issues", [])}
    added = [i for i in mine.get("issues", []) if i.get("file") not in live_files]
    if not added:
        print("[reconcile] manifest.json — 원격에 이미 우리 항목이 있음(추가 0건)")
        return 0

    # 호수는 원격 기준으로 다시 매긴다. 원격이 그 사이 다른 호를 발행했을 수 있다.
    nxt = max((i.get("no", 0) for i in live.get("issues", [])), default=0) + 1
    for it in added:
        old = it.get("no")
        it["no"] = nxt
        if old != nxt:
            print(f"[reconcile] 호수 재배정: 제{old}호 → 제{nxt}호 ({it.get('file')})")
        nxt += 1
        live["issues"].append(it)

    jdump(live_p, live)
    print(f"[reconcile] manifest.json — {len(added)}건 append "
          f"(원격 {len(live_files)}건 보존, 총 {len(live['issues'])}건)")
    return len(added)


def reconcile_newsletters(backup_dir):
    """원격에 없는 기사 파일만 복사한다."""
    src = os.path.join(backup_dir, "newsletters")
    dst = os.path.join(REPO, "newsletters")
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        if not name.endswith(".html"):
            continue
        if os.path.exists(os.path.join(dst, name)):
            continue          # 원격에 이미 있음 — 덮어쓰지 않는다
        shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
        print(f"[reconcile] 기사 복원: newsletters/{name}")
        n += 1
    if not n:
        print("[reconcile] newsletters — 복원할 신규 파일 없음")
    return n


def _kimchi_key(rec):
    return (rec.get("date"), rec.get("mode"))


def reconcile_data(backup_dir):
    """data/*.json 이력을 합집합으로 병합한다(같은 키는 원격 값 유지)."""
    src = os.path.join(backup_dir, "data")
    dst = os.path.join(REPO, "data")
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    total = 0
    for name in sorted(os.listdir(src)):
        if not name.endswith(".json"):
            continue
        sp, dp = os.path.join(src, name), os.path.join(dst, name)
        mine = jload(sp)
        live = jload(dp)
        if mine is None:
            continue
        if live is None:                      # 원격에 아예 없던 파일
            shutil.copy2(sp, dp)
            print(f"[reconcile] data/{name} 신규 복사")
            continue
        if not (isinstance(mine, list) and isinstance(live, list)):
            print(f"[reconcile] data/{name} — 리스트가 아니라 병합 생략(원격 유지)")
            continue
        seen = {_kimchi_key(r) for r in live if isinstance(r, dict)}
        add = [r for r in mine if isinstance(r, dict) and _kimchi_key(r) not in seen]
        if not add:
            print(f"[reconcile] data/{name} — 추가 0건")
            continue
        # 정렬하지 않는다. daily_news.py도 append 순서를 그대로 쓰므로
        # 여기서 재정렬하면 파일 전체가 diff로 잡힌다.
        live.extend(add)
        jdump(dp, live, indent=1)
        print(f"[reconcile] data/{name} — {len(add)}건 병합(총 {len(live)}건)")
        total += len(add)
    return total


def main():
    if len(sys.argv) < 2:
        print("사용법: python tools/reconcile_publish.py <백업디렉터리>", file=sys.stderr)
        return 2
    backup = sys.argv[1]
    if not os.path.isdir(backup):
        print(f"[reconcile] 백업 디렉터리 없음: {backup} — 할 일 없음")
        return 0
    print(f"[reconcile] 원격 리셋 상태에 산출물 재적용 시작 (백업: {backup})")
    a = reconcile_newsletters(backup)
    b = reconcile_manifest(backup)
    c = reconcile_data(backup)
    print(f"[reconcile] 완료 — 기사 {a}건, manifest {b}건, data {c}건 재적용")
    print("[reconcile] index.html은 호출자가 tools/build_site.py로 재생성한다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
