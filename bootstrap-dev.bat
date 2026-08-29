@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3.11") else (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo [Local AI Stack] Creating virtual environment...
  %PY% -m venv .venv || goto :error
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip || goto :error
pip install -e ".[dev]" || goto :error
echo.
echo Development environment ready. Run: run.bat
exit /b 0
:error
echo Bootstrap failed.
exit /b 1
