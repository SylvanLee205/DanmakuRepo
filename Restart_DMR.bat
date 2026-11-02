@echo off
rem ASCII ONLY - do not add non-ASCII characters.
rem Restart DMR the safe way: stop watchdog -> graceful stop -> start again.
rem
rem Why not just close the window: Start_Render.bat is a watchdog loop that
rem respawns the program 10s after it exits. And killing python outright
rem leaves orphan "[recording]" segments (that cost 1.8 GB on 2026-10-07).
rem tools/restart_dmr.py does it in the right order and verifies the restart.
chcp 65001 <nul >nul
cd /d "%~dp0"
title DMR Restart
rem -- resolve the Python interpreter (venv / portable / py launcher / PATH) --
call "%~dp0_env.bat"
if errorlevel 1 (
  echo [X] No Python 3 found on this machine.
  echo     Run Setup.bat once ^(double-click it^) - it installs everything.
  pause
  exit /b 1
)
"%DMR_PY%" tools\restart_dmr.py %*
echo.
pause
