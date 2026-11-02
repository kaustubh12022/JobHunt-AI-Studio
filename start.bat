@echo off
title JobHunt AI Studio Launcher
cd /d "%~dp0"
echo ===================================================
echo           Launching JobHunt AI Studio
echo ===================================================
echo.

if exist ".venv\Scripts\python.exe" (
    echo Starting Streamlit via isolated .venv...
    ".venv\Scripts\python.exe" -m streamlit run app.py
) else (
    echo [ERROR] Virtual environment (.venv) was not found!
    echo Please run setup first.
    pause
)
