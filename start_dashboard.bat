@echo off
REM ===================================================================
REM  Stock Sentiment Tracker - launch the local dashboard
REM  Double-click this file. It starts the server and opens your browser.
REM ===================================================================
setlocal
cd /d "%~dp0"

set "PORT=8787"
set "PY=.venv\Scripts\python.exe"

REM Fall back to system Python if the venv isn't set up yet.
if not exist "%PY%" (
    echo [!] venv not found at %PY%, using system "python".
    echo     If imports fail, run:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    set "PY=python"
)

echo Starting dashboard on http://127.0.0.1:%PORT%  ...
REM Open the browser shortly after the server starts.
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:%PORT%"

REM Run the server in this window (close the window or press Ctrl+C to stop).
"%PY%" dashboard.py --port %PORT%

echo.
echo Dashboard stopped.
pause
endlocal
