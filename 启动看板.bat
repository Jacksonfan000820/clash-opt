@echo off
title clash-opt 代理链路监控
cd /d "%~dp0"

netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   监控已经在运行，正在打开看板...
  start "" http://127.0.0.1:8787
  timeout /t 2 /nobreak >nul
  exit /b
)

echo.
echo   ============================================================
echo     clash-opt   代理链路监控 / 自动优选
echo   ============================================================
echo.
echo   浏览器会自动打开:  http://127.0.0.1:8787
echo   关闭本窗口 = 停止监控
echo.

if not exist "clash_opt.py" (
  echo   [错误] 找不到 clash_opt.py，请确认本文件与它同目录
  pause
  exit /b 1
)

start "" http://127.0.0.1:8787
python -X utf8 clash_opt.py serve
echo.
echo   监控已停止。
pause
