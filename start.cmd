@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on PATH.
  echo   Install Python 3.11 or newer from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

python app\main.py %*
if errorlevel 1 (
  echo.
  echo   wow-vendor-flip exited with an error.
  pause
  exit /b 1
)
