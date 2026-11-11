@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not "%~1"=="" goto run
echo Перед проверкой закройте пульт плоттера.
echo 1 - Проверить Wi-Fi и экран DWIN
echo 2 - Найти адрес платы через USB
choice /C 12 /N /M "Выберите 1 или 2: "
if errorlevel 255 goto done
if errorlevel 2 goto usb
:run
"%~dp0PROGRAM\.venv\Scripts\python.exe" "%~dp0setup_check.py" %*
goto done
:usb
"%~dp0PROGRAM\.venv\Scripts\python.exe" "%~dp0setup_check.py" --usb
:done
pause
