@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || python -m venv .venv || (echo Python 3.11+ is required. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
echo.
echo Alfred is installed. It now starts with Windows; press Ctrl+Alt+L anywhere to open it.
start "" ".venv\Scripts\pythonw.exe" -m alfred
