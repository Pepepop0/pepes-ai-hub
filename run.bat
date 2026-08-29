@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run bootstrap-dev.bat first. & exit /b 1)
".venv\Scripts\python.exe" -m local_ai_stack
