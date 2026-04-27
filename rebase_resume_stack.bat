@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\rebase_resume_stack.ps1" %*
exit /b %ERRORLEVEL%
