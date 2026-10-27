@echo off
title CM SF Sync
cd /d "%~dp0"
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
:loop
python -I sync_service.py
if %errorlevel%==3 goto :eof
timeout /t 15 /nobreak >nul
goto loop
