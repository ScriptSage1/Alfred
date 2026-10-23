@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || python -m venv .venv || (echo Python 3.11+ is required. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
rem The Start menu shortcut and the sign-in entry run .venv\Scripts\pythonw.exe, so Alfred never shows a console window.
".venv\Scripts\python.exe" -c "from alfred import config, startup; startup.install(config.load())" || (pause & exit /b 1)
echo.
echo Alfred is installed: it is in the Start menu and starts with Windows. Press Ctrl+Alt+L anywhere to open it.
start "" ".venv\Scripts\pythonw.exe" -m alfred
