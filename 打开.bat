@echo off
chcp 65001 >nul
set PORT=8765
if not "%~1"=="" set PORT=%~1

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo.
    echo   服务没有在运行 [port %PORT%]
    echo   请先双击 启动.bat 启动服务，再运行本文件。
    echo.
    pause
    exit /b 1
)

echo Opening http://127.0.0.1:%PORT%/
start "" "http://127.0.0.1:%PORT%/"
ping -n 2 127.0.0.1 >nul
