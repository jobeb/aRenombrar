@echo off
cd /d "%~dp0"
py main.py 2> error.log
if errorlevel 1 (
    echo.
    echo === ERROR ===
    type error.log
    echo.
    pause
)
