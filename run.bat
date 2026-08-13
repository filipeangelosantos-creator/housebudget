@echo off
REM Run HouseBudget on Windows.
REM
REM   run.bat                  -^> http://localhost:8000  (this computer only)
REM   set HOST=0.0.0.0 ^& run.bat   also reachable from phones on your wi-fi
REM   set PORT=9000 ^& run.bat      use a different port
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- find Python -----------------------------------------------------------
set PYEXE=
where py >nul 2>&1
if %errorlevel%==0 set PYEXE=py
if "!PYEXE!"=="" (
  where python >nul 2>&1
  if !errorlevel!==0 set PYEXE=python
)
if "!PYEXE!"=="" (
  echo.
  echo   Python was not found.
  echo.
  echo   Install Python 3.11 or newer from https://www.python.org/downloads/
  echo   During setup, tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

!PYEXE! -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo.
  echo   Your Python is too old. HouseBudget needs 3.11 or newer.
  echo   Get it from https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

REM --- one-time setup --------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Setting up for the first time. This takes a minute...
  echo.
  !PYEXE! -m venv .venv
  if errorlevel 1 (
    echo   Could not create the environment in .venv
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Could not install the dependencies. Check your internet connection
    echo   and run this again.
    echo.
    pause
    exit /b 1
  )
)

REM --- where the data lives --------------------------------------------------
for /f "usebackq delims=" %%i in (`".venv\Scripts\python.exe" -c "from app import config; print(config.DATA_DIR)"`) do set DATA_DIR=%%i

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo.
echo   HouseBudget
echo   -----------
echo   Open:   http://localhost:%PORT%
if "%HOST%"=="0.0.0.0" (
  for /f "usebackq delims=" %%i in (`".venv\Scripts\python.exe" -m app.hostinfo`) do set LAN_IP=%%i
  if not "!LAN_IP!"=="" echo   Phone:  http://!LAN_IP!:%PORT%   ^(same wi-fi, plain HTTP^)
)
echo   Data:   %DATA_DIR%
echo           your database and statements - back this up, it is not in git
echo.
echo   Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT% --reload
endlocal
