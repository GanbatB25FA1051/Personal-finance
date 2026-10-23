@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto error
    call .venv\Scripts\activate.bat
    python -m pip install -r requirements.txt
    if errorlevel 1 goto error
) else (
    call .venv\Scripts\activate.bat
)
python -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
if errorlevel 1 goto error
exit /b 0
:error
echo Application setup or startup failed. See the error above.
pause
exit /b 1
