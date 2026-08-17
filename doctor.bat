@echo off
REM Work out why HouseBudget is not showing the data you expect.
REM
REM   doctor.bat
REM
REM Prints which database this checkout would open, every other one it can
REM find, and what is holding the port. Reads only; changes nothing.
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   HouseBudget doctor
echo   ==================
echo.

REM --- what this checkout would open -----------------------------------------
set PYEXE=
if exist ".venv\Scripts\python.exe" set PYEXE=.venv\Scripts\python.exe
if "!PYEXE!"=="" (
  where py >nul 2>&1
  if !errorlevel!==0 set PYEXE=py
)
if "!PYEXE!"=="" (
  where python >nul 2>&1
  if !errorlevel!==0 set PYEXE=python
)

if "!PYEXE!"=="" (
  echo   [!] Python not found, so the resolved data directory cannot be shown.
) else (
  echo   Started from this window, the app would use:
  echo.
  !PYEXE! -m app.doctor
)

echo.
echo   Every budget.db this account can see
echo   ------------------------------------
for %%D in (
  "%USERPROFILE%\.housebudget"
  "%~dp0data"
  "C:\Windows\System32\config\systemprofile\.housebudget"
  "C:\ProgramData\HouseBudget"
) do (
  if exist "%%~D\budget.db" (
    for %%F in ("%%~D\budget.db") do echo     %%~zF bytes  %%~tF  %%~fF
  ) else (
    echo     -                          not there:    %%~D\budget.db
  )
)

echo.
echo   HB_DATA_DIR   = %HB_DATA_DIR%
echo   HB_DB_PATH    = %HB_DB_PATH%
echo   (blank means the app picks the location itself)

echo.
echo   What is listening
echo   -----------------
netstat -ano | findstr /R /C:":8000 " /C:":8010 " /C:":8080 "
if errorlevel 1 echo     nothing on 8000, 8010 or 8080

echo.
echo   Service
echo   -------
sc query HouseBudgetService >nul 2>&1
if errorlevel 1 (
  echo     no HouseBudgetService installed
) else (
  sc qc HouseBudgetService | findstr /C:"SERVICE_START_NAME" /C:"BINARY_PATH_NAME" /C:"START_TYPE"
  sc query HouseBudgetService | findstr /C:"STATE"
  echo.
  echo     A service running as LocalSystem has its own home directory, so it
  echo     opens C:\Windows\System32\config\systemprofile\.housebudget unless
  echo     HB_DATA_DIR is set machine-wide with: setx /M HB_DATA_DIR "..."
)

echo.
pause
endlocal
