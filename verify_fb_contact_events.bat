@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\verify_fb_contact_events.ps1" %*
exit /b %ERRORLEVEL%
