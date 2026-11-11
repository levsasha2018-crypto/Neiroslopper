@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run INSTALL.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-camera.txt
if errorlevel 1 goto fail
echo Camera, scanner and image tracing dependencies are ready.
pause
exit /b 0
:fail
echo Installation failed. Check Internet access and Python version.
pause
exit /b 1
