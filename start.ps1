# JobHunt AI Studio PowerShell Launcher
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "          Launching JobHunt AI Studio             " -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    Write-Host "Starting Streamlit via isolated .venv..." -ForegroundColor Green
    & $VenvPython -m streamlit run app.py
} else {
    Write-Host "[ERROR] Virtual environment (.venv) not found at: $VenvPython" -ForegroundColor Red
}
