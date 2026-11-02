@echo off
chcp 65001 >nul
title QuantitativeTrading 2.0 后台量化服务
echo ======================================================================
echo    A股智能量化系统 2.0 (QuantitativeTrading 2.0)
echo    已激活开机自检、数据断档自动回溯与 Windows 交易时段防休眠引擎
echo ======================================================================

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)
pause
