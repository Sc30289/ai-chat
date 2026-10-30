@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0..\minimind-vscode\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Using: %PY%
"%PY%" server.py %*
echo.
echo [server exited] code=%errorlevel%
pause
