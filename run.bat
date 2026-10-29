@echo off
cd /d "%~dp0"
if not defined FINANCE_APP_HEADLESS set "FINANCE_APP_HEADLESS=false"
if not defined FINANCE_APP_PORT set "FINANCE_APP_PORT=8501"
set "APP_PYTHON="
python -c "import streamlit, pandas, openpyxl" >nul 2>nul
if not errorlevel 1 (
    set "APP_PYTHON=python"
    echo Using the installed Python environment.
    goto launch
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv --system-site-packages .venv
    if errorlevel 1 goto missing_dependencies
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -c "import streamlit, pandas, openpyxl" >nul 2>nul
    if not errorlevel 1 set "APP_PYTHON=.venv\Scripts\python.exe"
)
if not defined APP_PYTHON goto missing_dependencies
:launch
if defined FINANCE_APP_PASSWORD (
    "%APP_PYTHON%" -m streamlit run app.py --server.headless %FINANCE_APP_HEADLESS% --server.port %FINANCE_APP_PORT% --browser.gatherUsageStats false
) else (
    powershell -NoProfile -Command "$secure = Read-Host 'My Finance application password' -AsSecureString; $env:FINANCE_APP_PASSWORD = (New-Object System.Net.NetworkCredential('', $secure)).Password; & '%APP_PYTHON%' -m streamlit run app.py --server.headless %FINANCE_APP_HEADLESS% --server.port %FINANCE_APP_PORT% --browser.gatherUsageStats false; exit $LASTEXITCODE"
)
if errorlevel 1 goto error
exit /b 0
:missing_dependencies
echo Neither the installed Python nor the project environment can load Streamlit and pandas.
echo Ask your administrator to approve the Python package location. Do not disable Windows Application Control.
goto error
:error
echo Application setup or startup failed. See the error above.
pause
exit /b 1
