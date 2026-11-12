@echo off
chcp 65001 >nul
title QuantitativeTrading 2.0 停止服务
cd /d "%~dp0"
echo ======================================================================
echo    正在结束占用 Web 端口的量化服务进程...
echo ======================================================================
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py --stop
) else (
    python main.py --stop
)
echo.
pause
