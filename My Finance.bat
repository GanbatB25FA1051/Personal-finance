@echo off
setlocal
set "APP_URL=https://personal-finance-ohhq.onrender.com"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%LocalAppData%\Microsoft\Edge\Application\msedge.exe"

if exist "%EDGE%" (
    start "" "%EDGE%" --app="%APP_URL%" --no-first-run
    exit /b 0
)

where chrome.exe >nul 2>nul
if not errorlevel 1 (
    start "" chrome.exe --app="%APP_URL%"
    exit /b 0
)

start "" "%APP_URL%"
echo Opened My Finance in your default browser.
echo Install Microsoft Edge or Google Chrome for an app-style window.
pause
