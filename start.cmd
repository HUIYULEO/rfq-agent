@echo off
cd /d "%~dp0"
title MatrixNEXT RFQ Agent

where py >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")

%PY% --version >nul 2>nul
if errorlevel 1 goto nopython

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

rem Check dependencies the way the app does: the .deps/ cache counts as installed.
%PY% -m rfq_agent.checkenv >nul 2>nul
if errorlevel 1 (
  echo First run: installing python-docx and openpyxl ...
  %PY% -m pip install -e ".[documents]"
  if errorlevel 1 goto nodeps
)

echo.
echo   MatrixNEXT RFQ Agent  --^>  http://127.0.0.1:8765/
echo   Your browser opens automatically. Close this window to stop the service.
echo.
start "" http://127.0.0.1:8765/
%PY% -m rfq_agent.web --port 8765
pause
exit /b 0

:nopython
echo Python was not found. Install Python 3.11 or newer, then run this file again.
pause
exit /b 1

:oldpython
echo Python 3.11 or newer is required.
pause
exit /b 1

:nodeps
echo Could not install dependencies. Run manually:  pip install -e ".[documents]"
pause
exit /b 1
