@echo off
setlocal
cd /d "%~dp0"
set "PLOTTER_VOICE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PLOTTER_VOICE_PYTHON%" set "PLOTTER_VOICE_PYTHON=%~dp0..\..\..\plotter_v1\.venv\Scripts\python.exe"
if exist "%~dp0..\..\..\plotter_v1\assistant\voice_models" set "PLOTTER_VOICE_MODEL_ROOT=%~dp0..\..\..\plotter_v1\assistant\voice_models"
if not exist "%PLOTTER_VOICE_PYTHON%" (
    echo Run INSTALL.cmd first.
    pause
    exit /b 1
)
"%PLOTTER_VOICE_PYTHON%" -m pip install --only-binary=:all: -r assistant\requirements-voice.txt
if errorlevel 1 goto failed
"%PLOTTER_VOICE_PYTHON%" -X utf8 -m assistant.voice_setup --profile all
if errorlevel 1 goto failed
echo Voice input is ready. Restart the assistant.
pause
exit /b 0
:failed
echo Voice installation failed.
pause
exit /b 1
