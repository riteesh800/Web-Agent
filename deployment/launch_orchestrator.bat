@echo off
title Web Agent Hub
echo ========================================================
echo Launching Web Agent Hub (Edge AI Orchestrator)...
echo ========================================================
cd /d "%~dp0\.."
if exist "..\venv\Scripts\python.exe" (
    ..\venv\Scripts\python.exe bootstrap.py
) else (
    python bootstrap.py
)
pause
