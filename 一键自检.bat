@echo off
chcp 65001 >nul
title clash-opt self check
cd /d "%~dp0"
python -X utf8 clash_opt.py diag
echo.
echo   dashboard: http://127.0.0.1:8787
pause
