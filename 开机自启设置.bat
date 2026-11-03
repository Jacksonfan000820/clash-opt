@echo off
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

echo.
echo   当前状态：
if exist "%STARTUP%\clash-opt.bat" (echo     [已启用] 开机自动启动监控) else (echo     [未启用] 开机不会自动启动)

echo.
echo   1 = 启用开机自启
echo   2 = 停用开机自启
echo   3 = 立即启动一次（不设自启）
echo   4 = 打开看板
echo   0 = 退出
echo.
set /p CH=  请输入数字后回车:

if "%CH%"=="1" (
  copy /y "%~dp0autostart.bat" "%STARTUP%\clash-opt.bat" >nul
  echo   已启用。下次登录会自动运行。
)
if "%CH%"=="2" (
  if exist "%STARTUP%\clash-opt.bat" del "%STARTUP%\clash-opt.bat"
  echo   已停用。
)
if "%CH%"=="3" (
  netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
  if errorlevel 1 (
    start "" /b "D:\Python\pythonw.exe" -X utf8 "%~dp0clash_opt.py" serve
    timeout /t 3 /nobreak >nul
    echo   已启动。
  ) else (
    echo   已经在运行了。
  )
)
if "%CH%"=="4" start "" "http://127.0.0.1:8787"

echo.
pause
