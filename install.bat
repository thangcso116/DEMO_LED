@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Installation complete. Run start.bat to launch MohuanLED RGB Studio.
pause
exit /b 0
:fail
echo.
echo Installation failed. Check that Python 3.10+ is installed and added to PATH.
pause
exit /b 1
