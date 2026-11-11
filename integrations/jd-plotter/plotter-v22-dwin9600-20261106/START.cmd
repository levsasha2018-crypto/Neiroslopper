@echo off
cd /d "%~dp0"
if not exist "PROGRAM\.venv\Scripts\pythonw.exe" (
  echo Run PROGRAM\INSTALL.cmd first.
  exit /b 1
)
start "" "PROGRAM\.venv\Scripts\pythonw.exe" "launch.py"
