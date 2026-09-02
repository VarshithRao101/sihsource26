@echo off
REM ---------------------------------------------------------------------
REM  SIH26161 - start the operator console.
REM  Double-click this, wait for "Application startup complete", then open
REM  http://localhost:8000 in a browser.
REM
REM  Keep this window OPEN. Closing it stops the server.
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

echo.
echo   Starting the dam break console...
echo   It warms the solver and the ML surrogate first, so give it ~20 seconds.
echo   When you see "Application startup complete", open:
echo.
echo       http://localhost:8000
echo.
echo   Close this window to stop the server.
echo.

.venv\Scripts\python.exe -m uvicorn modules.04_backend.api:app --port 8000

echo.
echo   Server stopped.
pause
