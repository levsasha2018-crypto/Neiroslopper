@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 goto fail
if exist "..\OFFLINE_WHEELS" (
  ".venv\Scripts\python.exe" -m pip install --no-index --find-links "..\OFFLINE_WHEELS" -r requirements.txt
) else (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if errorlevel 1 goto fail
echo Ready. Run START.cmd.
pause
exit /b 0
:fail
echo Installation failed. Install Windows Python 3.11 or newer with Tk.
echo The offline wheels support Windows x64 Python 3.11 through 3.14.
echo If needed, install the requirements from the Internet manually.
pause
exit /b 1
