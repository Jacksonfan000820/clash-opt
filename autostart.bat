@echo off
rem ===========================================================
rem  clash-opt autostart (runs at logon)
rem  1. make sure Clash Verge core is up, launch it if not
rem  2. then start the monitor service
rem  To disable: delete this file, or run "autostart settings.bat"
rem ===========================================================
setlocal
set "APP=D://projects//clash-opt"
set "VERGE=D://Clashverge//clash-verge.exe"
cd /d "%APP%"

rem ---- monitor already running? then exit ----
netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 exit /b

rem ---- Clash core not up? launch Clash Verge first ----
netstat -ano | findstr ":9097" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  if exist "%VERGE%" start "" "%VERGE%"
  for /l %%i in (1,1,20) do (
    timeout /t 3 /nobreak >nul
    netstat -ano | findstr ":9097" | findstr "LISTENING" >nul 2>&1
    if not errorlevel 1 goto ready
  )
)
:ready

rem ---- start monitor ----
rem DO NOT use "start /b": it binds the child to this cmd console, and when
rem the parent exits the child gets BrokenPipe on next stdout write and dies
rem silently. Redirect output to a file instead.
if exist "D://Python//pythonw.exe" (
  start "" "D://Python//pythonw.exe" -X utf8 "%APP%\clash_opt.py" serve >> "%APP%\serve.log" 2>&1
) else (
  start "" pythonw -X utf8 "%APP%\clash_opt.py" serve >> "%APP%\serve.log" 2>&1
)
exit /b
