# -*- coding: utf-8 -*-
"""
publish.py — INVEST STORY 한 커맨드 발간 도구
PDF 하나를 사이트에 올린다: newsletters/ 복사 → manifest 갱신 → index.html 재생성 → git 커밋·푸시.

사용 예)
  python tools/publish.py "newsletter_20260619_special_v2.pdf" --tag 특집호 ^
         --title "코스피 9,000 시대 — 상승·하락·횡보 3대 시나리오 정밀 분석" ^
         --summary "사상 첫 9,000 돌파. 12개 요인으로 세 시나리오를 분해했습니다."

옵션을 안 주면: 같은 이름의 .json(사이드카)에서 읽거나, 파일명에서 날짜(YYYYMMDD)를 자동 인식한다.
--no-push 를 주면 git 작업 없이 로컬에서 index.html 만 갱신(미리보기용).
"""
import argparse, json, os, re, shutil, subprocess, sys, datetime

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT  = os.path.dirname(TOOLS)                       # repo root
NEWSDIR = os.path.join(ROOT, "newsletters")
MANIFEST = os.path.join(ROOT, "manifest.json")
DOMAIN = "investstory.co.kr"
BRANCH = "main"          # GitHub Pages 브랜치 (필요시 master 로 변경)

def run(cmd):
    print("  $", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

def parse_date(pdf, given):
    if given: return given
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", os.path.basename(pdf))
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.date.today().isoformat()

def load_sidecar(pdf):
    side = os.path.splitext(pdf)[0] + ".json"
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f: return json.load(f)
    return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="발간할 PDF 경로")
    ap.add_argument("--title"); ap.add_argument("--summary")
    ap.add_argument("--tag", default="데일리")
    ap.add_argument("--date"); ap.add_argument("--no", type=int)
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.pdf):
        sys.exit(f"[오류] PDF를 찾을 수 없습니다: {a.pdf}")
    side = load_sidecar(a.pdf)
    title   = a.title   or side.get("title")   or "투자이야기 리포트"
    summary = a.summary or side.get("summary") or ""
    tag     = a.tag     if a.tag != "데일리"   else side.get("tag", "데일리")
    date    = parse_date(a.pdf, a.date or side.get("date"))

    os.makedirs(NEWSDIR, exist_ok=True)
    with open(MANIFEST, encoding="utf-8") as f: man = json.load(f)
    issues = man.setdefault("issues", [])

    no = a.no or side.get("no") or (max([i.get("no",0) for i in issues], default=0) + 1)

    # 같은 날짜 파일 충돌 시 -2, -3 ... 부여
    base = date; fname = f"{base}.pdf"; k = 2
    while os.path.exists(os.path.join(NEWSDIR, fname)):
        fname = f"{base}-{k}.pdf"; k += 1
    shutil.copy2(a.pdf, os.path.join(NEWSDIR, fname))
    rel = f"newsletters/{fname}"
    print(f"[1/4] PDF 복사 → {rel}")

    # manifest 맨 앞에 추가(중복 file 제거)
    issues[:] = [i for i in issues if i.get("file") != rel]
    issues.insert(0, {"no":no, "date":date, "tag":tag, "title":title, "summary":summary, "file":rel})
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print(f"[2/4] manifest 갱신 → 제 {no}호 ({date})")

    # GitHub Pages 보조 파일 보장
    nojekyll = os.path.join(ROOT, ".nojekyll")
    if not os.path.exists(nojekyll): open(nojekyll,"w").close()
    cname = os.path.join(ROOT, "CNAME")
    if not os.path.exists(cname):
        with open(cname,"w") as f: f.write(DOMAIN+"\n")

    # index.html 재생성
    sys.path.insert(0, TOOLS); import build_site; build_site.main()
    print("[3/4] index.html 재생성 완료")

    if a.no_push:
        print("[4/4] --no-push: git 작업 건너뜀 (로컬 미리보기 only)")
        print(f"\n로컬 미리보기:  cd {ROOT} && python -m http.server 8080  →  http://localhost:8080")
        return
    branch = subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip() or "main"
    try:
        run(["git","add","-A"])
        run(["git","commit","-m",f"publish: 제{no}호 ({date}) {title}"])
        run(["git","push","origin",branch])
    except subprocess.CalledProcessError as e:
        sys.exit(f"[오류] git 작업 실패: {e}\n  - 원격 연결(git remote -v)과 GitHub 로그인 상태를 확인하세요.")
    print(f"[4/4] 발간 완료 ✦  약 1분 후 https://{DOMAIN} 에 반영됩니다.")

if __name__ == "__main__":
    main()
