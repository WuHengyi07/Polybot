@echo off
REM Windows auto-restart wrapper for the bot service.
REM Register with Task Scheduler ("At startup", run this .bat) for unattended operation.
cd /d "%~dp0\.."
:loop
python main.py service
echo [%date% %time%] service exited, restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
