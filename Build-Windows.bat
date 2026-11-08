@echo off
call "%~dp0Start-Windows.bat" --build
exit /b %errorlevel%
