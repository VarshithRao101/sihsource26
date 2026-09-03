@echo off
REM ---------------------------------------------------------------------
REM  SIH26161 - the real solver, reachable from the Vercel pages.
REM
REM  WHY THIS EXISTS. Vercel can host the two pages but it cannot run the
REM  backend: serverless functions freeze when they return, have no writable
REM  disk, do not do WebSockets, and cap the bundle at 250 MB against the
REM  5.6 GB this project installs. So the pages are deployed there and the
REM  SOLVER STAYS HERE, on this machine, exposed over HTTPS by a Cloudflare
REM  tunnel. Everything then works - PLAY, the live WebSocket, the 3D scene,
REM  the point query, .shp and .kml - because the real backend is answering.
REM
REM  A browser will not let an https:// page call http:// or ws://, which is
REM  the entire reason for the tunnel. It is not about being reachable from
REM  the internet; it is about being reachable over TLS.
REM
REM  USAGE:  start_public.bat https://your-app.vercel.app
REM          (or set SIH_VERCEL_ORIGIN once and just double-click this)
REM
REM  Then read the https://<random>.trycloudflare.com URL out of the tunnel
REM  window and open:
REM
REM      https://your-app.vercel.app/?api=https://<random>.trycloudflare.com
REM
REM  config.js remembers that base in localStorage, so the Workflow page picks
REM  it up too and you only paste it once. Open with "?api=" to clear it.
REM
REM  Keep BOTH windows open. Closing either one ends the demo.
REM ---------------------------------------------------------------------
cd /d "%~dp0"

set "ORIGIN=%~1"
if "%ORIGIN%"=="" set "ORIGIN=%SIH_VERCEL_ORIGIN%"

if "%ORIGIN%"=="" (
  echo.
  echo   ERROR: no Vercel origin given.
  echo.
  echo   The backend has to be told which origin is allowed to call it, or the
  echo   browser blocks every request the page makes and the console just sits
  echo   there saying "backend unreachable".
  echo.
  echo       start_public.bat https://your-app.vercel.app
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   ERROR: .venv not found in %CD%
  echo   Create it first:  python -m venv .venv
  echo   then:             .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo.
  echo   ERROR: cloudflared is not on PATH.
  echo.
  echo   Install it with:   winget install --id Cloudflare.cloudflared
  echo   or download from:  https://github.com/cloudflare/cloudflared/releases
  echo.
  echo   Without it the pages are https and this backend is http, and the
  echo   browser will refuse the call. There is no way around that.
  echo.
  pause
  exit /b 1
)

REM Localhost stays allowed so start_public.bat and a local browser both work.
set "SIH_CORS_ORIGINS=%ORIGIN%,http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173"

echo.
echo   Allowed origin : %ORIGIN%
echo   Opening the Cloudflare tunnel in a second window...
echo   Copy the https://...trycloudflare.com URL it prints, then open:
echo.
echo       %ORIGIN%/?api=https://^<that-url^>
echo.

start "cloudflare tunnel - keep open" cmd /k cloudflared tunnel --url http://localhost:8000

echo   Starting the solver. It warms the JIT and the surrogate first, ~20 s.
echo   Close this window to stop the server.
echo.

.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000

echo.
echo   Server stopped. The tunnel window is still open - close it too.
pause
