@echo off
REM ---------------------------------------------------------------------
REM  SIH26161 - 1-Click Live Public Demo (Solver + Cloudflare Tunnel)
REM  Double-click this file to start the backend and generate a public HTTPS
REM  link for judges / evaluators to open on any device.
REM ---------------------------------------------------------------------
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   ERROR: .venv not found in %CD%
  echo   Create it first:  python -m venv .venv
  echo   then:             .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

set "CLOUDFLARED_EXE="
where cloudflared >nul 2>&1
if not errorlevel 1 (
  set "CLOUDFLARED_EXE=cloudflared"
) else if exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" (
  set "CLOUDFLARED_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
) else if exist "C:\Program Files\cloudflared\cloudflared.exe" (
  set "CLOUDFLARED_EXE=C:\Program Files\cloudflared\cloudflared.exe"
)

if "%CLOUDFLARED_EXE%"=="" (
  echo.
  echo   ERROR: cloudflared is not found.
  echo   Run: winget install --id Cloudflare.cloudflared
  echo.
  pause
  exit /b 1
)

set "SIH_CORS_ORIGINS=*"

echo.
echo =========================================================================
echo   SIH26161 - Dam Break Inundation Public Demo
echo =========================================================================
echo.
echo   [1/2] Launching Cloudflare Tunnel in a second window...
echo         Look at that window to see your public https://...trycloudflare.com link.
echo.

start "Cloudflare Tunnel - Public HTTPS Link" cmd /k ""%CLOUDFLARED_EXE%" tunnel --url http://localhost:8000"

echo   [2/2] Starting hydrodynamic solver and ML surrogate (~15-20 s warm-up)...
echo         Keep BOTH windows open. Closing either one stops the demo.
echo.

.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --host 0.0.0.0 --port 8000

echo.
echo Server stopped. Close the tunnel window as well.
pause
