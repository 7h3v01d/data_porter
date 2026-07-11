@echo off
setlocal
cd /d "%~dp0"
title Data Porter 0.1.1 - Safe Guided Mode

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 data_porter_quick.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python data_porter_quick.py
    goto done
)

echo.
echo Python 3.11 or newer was not found on this PC.
echo Install Python, tick "Add Python to PATH", then run this file again.
echo.

:done
echo.
pause
endlocal
