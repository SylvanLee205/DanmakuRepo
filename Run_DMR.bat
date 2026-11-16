@echo off
rem ASCII ONLY. This is the ACTION of the boot scheduled task.
rem
rem --boot-fallback 180 behaviour (supervise mode, stays resident):
rem   * someone is logged in  -> do nothing; the logon autostart (Startup
rem     folder entry + DMR_SessionWatch task) starts DMR inside the
rem     user session, so the console window is VISIBLE on the desktop
rem     (the familiar behaviour).
rem   * nobody logged in       -> after the 180s grace period, start DMR here
rem     in session 0 so recording never stops, then keep watching: if DMR
rem     dies while nobody is logged in, start it again.
rem   * logged in but DMR dead -> wait 30 minutes (the logon autostart retries
rem     itself), then session 0 fallback as a last resort (recording first).
rem This mode NEVER kills or migrates processes - migration is done by the
rem logon autostart only, so the two never fight.
rem Every decision is logged to logs\autostart.log (session 0 has no console).
chcp 65001 <nul >nul
cd /d "%~dp0"
rem -- resolve the Python interpreter (venv / portable / py launcher / PATH) --
call "%~dp0_env.bat"
if errorlevel 1 (
  echo [X] No Python 3 found on this machine.
  echo     Run Setup.bat once ^(double-click it^) - it installs everything.
  rem NEVER pause here: this script also runs from the boot scheduled task in
  rem session 0, where there is no console. A pause would hang forever:
  rem no recording, no retry, and nothing in autostart.log.
  rem Write the reason to a file instead and exit so the task can be seen as failed.
  if not exist "%~dp0logs" mkdir "%~dp0logs"
  >>"%~dp0logs\startup_error.log" echo [%date% %time%] %~nx0: no Python 3 found, run Setup.bat
  exit /b 1
)
"%DMR_PY%" tools\autostart_launch.py --boot-fallback 180
