@echo off
chcp 65001 >nul
REM ============================================================
REM  INVEST STORY 발간 (드래그-드롭 또는 명령행)
REM  사용법 1) 이 파일에 PDF를 끌어다 놓기  → 그 PDF가 발간됨
REM  사용법 2) publish.bat "newsletter_20260619.pdf" --tag 특집호 --title "제목" --summary "요약"
REM ============================================================
cd /d "%~dp0\.."
if "%~1"=="" (
  echo PDF 파일을 이 배치파일에 끌어다 놓거나, 인자로 PDF 경로를 넘기세요.
  pause
  exit /b 1
)
python "%~dp0publish.py" %*
echo.
pause
