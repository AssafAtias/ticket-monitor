@echo off
REM Leave this window open. Ctrl+C stops the monitor.
title Ticket Monitor
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

:loop
python monitor.py
echo.
echo Monitor exited. Restarting in 15 seconds... (close this window to stop)
timeout /t 15 /nobreak >nul
goto loop
