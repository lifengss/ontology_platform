@echo off
rem Post-install dependency bootstrap launcher (started by NSIS via ExecWait).
rem Runs install-deps.ps1 from the install dir; if it fails, KEEPS THIS WINDOW
rem OPEN so the error is visible (prevents the silent "flash and close").
rem Keep this file pure ASCII (cmd reads it as ANSI).
setlocal
cd /d "%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-deps.ps1" -InstallDir "%~dp0.."
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [FAILED] install-deps.ps1 exited with code %RC%.
    echo Check logs\install.log and logs\install-result.ini in the install directory for details.
    echo Press any key to close this window...
    pause >nul
)
endlocal
exit /b %RC%
