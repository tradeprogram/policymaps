@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === [1/2] 최신 코드 받기 (git pull) ===
git pull

if not exist ".env" if not exist "system\.env" (
  echo.
  echo [!] .env 가 없습니다. AI(Gemini) 답변을 쓰려면 먼저 아래를 하세요:
  echo     1^) copy system\.env.example system\.env
  echo     2^) system\.env 를 메모장으로 열어  GEMINI_API_KEY=발급받은키  채우기
  echo     ^(키 없이도 사이트는 정상 동작하고, AI만 로컬 요약으로 대체됩니다^)
  echo.
  pause
)

echo.
echo === [2/2] AI 서버 시작 ===
echo 브라우저에서 열기:  http://localhost:8742/viz/public/index.html
echo 종료하려면 이 창에서 Ctrl+C
echo.
python viz\serve_ai.py --port 8742

pause
