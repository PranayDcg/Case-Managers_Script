@echo off
title CM Slack Watcher
cd /d "%~dp0"

:: Clear proxy vars
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=

:loop
echo [%date% %time%] Starting CM Slack Watcher...
python slack_watcher.py
echo [%date% %time%] Watcher stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
