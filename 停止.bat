@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set PORT=8765
if not "%~1"=="" set PORT=%~1

echo Stopping AI chat server on port %PORT% ...
set KILLED=0
for /l %%i in (1,1,3) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
        echo   killing process tree of PID %%a
        taskkill /PID %%a /T /F >nul 2>&1
        set KILLED=1
    )
    ping -n 2 127.0.0.1 >nul
)

if "!KILLED!"=="0" (
    echo   nothing was listening on port %PORT%.
) else (
    echo   done. if the port still shows up in netstat for a few seconds, that is just
    echo   Windows releasing the socket - it will free up on its own.
)
ping -n 3 127.0.0.1 >nul
