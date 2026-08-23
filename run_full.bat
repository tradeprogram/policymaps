@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM  자치법규 정책지도 — 완전판(로컬 DB 직결) 실행
REM
REM  배포본(Vercel 정적)에는 조문 '본문' 236만 건(약 490MB)이 들어가지 않는다.
REM  이 배치는 DB(system\data\policymap.db, 4.3GB)를 직접 읽는 서버를 띄운다.
REM
REM  사용법:  run_full.bat          (기본 포트 8743)
REM           run_full.bat 9000     (포트 지정)
REM ---------------------------------------------------------------------------

set PORT_ARG=8743
if not "%~1"=="" set PORT_ARG=%~1

if not exist "system\data\policymap.db" (
  echo.
  echo [!] system\data\policymap.db 가 없습니다.
  echo     완전판을 쓸 수 없어 화면이 정적 shard 로만 동작합니다
  echo     ^(조문 본문 대신 조문 제목까지만 보입니다^).
  echo.
  pause
)

echo === 완전판 서버 시작 ===
echo 브라우저에서 열기:  http://127.0.0.1:%PORT_ARG%/viz/public/index.html?full=1
echo.
echo RAG 전문검색 색인 예열에 2~3분 걸립니다 ^(실측 128초^).
echo 예열 중에도 조문 본문 조회와 그래프는 바로 됩니다.
echo 상태 확인:  http://127.0.0.1:%PORT_ARG%/api/db/status
echo 종료하려면 이 창에서 Ctrl+C
echo.

start "" "http://127.0.0.1:%PORT_ARG%/viz/public/index.html?full=1"
python viz\serve_full.py --port %PORT_ARG%

pause
