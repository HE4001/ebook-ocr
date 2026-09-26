@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ocr.ps1" %*
exit /b %ERRORLEVEL%
