@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0codex_usage_tray.py"
    exit /b
)
start "" pyw "%~dp0codex_usage_tray.py"
