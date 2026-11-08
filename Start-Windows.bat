@echo off
setlocal
set "error_message=Could not open the project folder."
pushd "%~dp0" || goto report_failure
set "error_message=Project files are missing. Extract the entire source ZIP first."
if not exist "pyproject.toml" goto failed
if not exist "app.py" goto failed

set "venv_python=.venv-win\Scripts\python.exe"
if exist "%venv_python%" goto install

set "python_check=import sys; raise SystemExit(not (sys.version_info[:2] >= (3, 11) and sys.maxsize > 2**32))"
set "python_command=py -3.13"
%python_command% -c "%python_check%" >nul 2>&1
if not errorlevel 1 goto create_venv
set "python_command=py -3.12"
%python_command% -c "%python_check%" >nul 2>&1
if not errorlevel 1 goto create_venv
set "python_command=py -3"
%python_command% -c "%python_check%" >nul 2>&1
if not errorlevel 1 goto create_venv
set "python_command=python"
%python_command% -c "%python_check%" >nul 2>&1
if not errorlevel 1 goto create_venv
set "error_message=64-bit Python 3.11 or newer was not found. Install Python 3.13 x64 with PATH enabled."
goto failed

:create_venv
set "error_message=Could not create the Python environment. See the error above."
%python_command% -m venv .venv-win || goto failed

:install
if /i "%~1"=="--build" goto build
if exist ".venv-win\.groupreader-ready" goto launch
set "error_message=Could not install dependencies. See the pip error above."
"%venv_python%" -m pip install -e . || goto failed
echo ready> .venv-win\.groupreader-ready

:launch
set "error_message=Could not start GroupChatReader. See the error above."
"%venv_python%" app.py || goto failed
popd
exit /b 0

:build
set "error_message=Could not install build dependencies. See the pip error above."
"%venv_python%" -m pip install -e ".[build]" || goto failed
echo ready> .venv-win\.groupreader-ready
set "error_message=Could not build GroupChatReader. See the error above."
"%venv_python%" -m PyInstaller --noconfirm GroupChatReader.spec || goto failed
echo Built dist\GroupChatReader-WinR4\GroupChatReader-WinR4.exe
pause
popd
exit /b 0

:failed
popd

:report_failure
echo.
echo %error_message%
pause
exit /b 1
