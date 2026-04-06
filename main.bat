@echo off
setlocal
chcp 65001 > nul

pushd "%~dp0"

set "BOOTSTRAP_SCRIPT=bootstrap_venv.py"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "EXIT_CODE=0"

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%BOOTSTRAP_SCRIPT%"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto :finish
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%BOOTSTRAP_SCRIPT%"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto :finish
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%BOOTSTRAP_SCRIPT%"
    set "EXIT_CODE=%ERRORLEVEL%"
    goto :finish
)

echo [ERROR] Python 3.10+ was not found.
echo [ERROR] Install Python 3.10 or newer and add it to PATH.
set "EXIT_CODE=1"

:finish
echo.
echo Task finished. Press any key to exit...
pause >nul
popd
exit /b %EXIT_CODE%
