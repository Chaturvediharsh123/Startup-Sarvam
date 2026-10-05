# Runs automatically when Windows Sandbox starts (see Startup-Sarvam.wsb).
# Installs Python, creates a virtual env INSIDE the sandbox, installs the
# dependencies and starts the assistant. The project folder is read-only, so all
# writable data goes to C:\sarvam-data inside the sandbox.

$ErrorActionPreference = "Stop"
$Project = "C:\Users\WDAGUtilityAccount\Desktop\Startup-Sarvam"
$Data = "C:\sarvam-data"
$Venv = "C:\sarvam-venv"
$PythonVersion = "3.12.10"
$Installer = "$env:TEMP\python-$PythonVersion-amd64.exe"

Write-Host "== Startup-Sarvam sandbox setup ==" -ForegroundColor Cyan

if (-not (Test-Path "$Project\.env")) {
    Write-Host "No .env in the project folder. Create it on the host first (copy .env.example)." -ForegroundColor Red
    return
}

Write-Host "Downloading Python $PythonVersion ..."
Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $Installer
Write-Host "Installing Python (quiet) ..."
Start-Process -FilePath $Installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0" -Wait
$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

Write-Host "Creating virtual env and installing dependencies ..."
& $Python -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Venv\Scripts\python.exe" -m pip install --quiet -r "$Project\requirements.txt"

New-Item -ItemType Directory -Force -Path $Data | Out-Null
$env:DB_PATH = "$Data\assistant.db"
$env:LOG_DIR = "$Data\logs"
$env:NOTES_DIR = "$Data\notes"

Set-Location $Project
Write-Host "Starting the assistant. Close the window to stop." -ForegroundColor Green
& "$Venv\Scripts\python.exe" main.py
