@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Prefer the bundled runtime, fall back to PATH python
set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

echo ==========================================
echo   CRM automation pipeline
echo ==========================================
"%PY%" pipeline.py %*

echo.
echo Report : out\report_YYYY-MM-DD.md
echo Board  : out\dashboard.html
echo DB     : ghl.db
echo.
pause
