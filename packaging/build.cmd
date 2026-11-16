@echo off
rem Build dist\Alfred (Alfred.exe and its _internal folder) and Alfred-<version>-win64.zip, outside the repository.
rem Output folder: %ALFRED_BUILD_DIR%, by default %USERPROFILE%\AlfredDigUpBench\exe_build (venv, build, dist, zip).
setlocal
for %%I in ("%~dp0..") do set "REPO=%%~fI"
if not defined ALFRED_BUILD_DIR set "ALFRED_BUILD_DIR=%USERPROFILE%\AlfredDigUpBench\exe_build"
set "OUT=%ALFRED_BUILD_DIR%"
set "PY=%OUT%\venv\Scripts\python.exe"
if not exist "%OUT%" mkdir "%OUT%" || exit /b 1

if not exist "%PY%" (
  echo Creating the build environment in %OUT%\venv
  py -3.14 -m venv "%OUT%\venv" || py -3 -m venv "%OUT%\venv" || (echo Python 3.11+ is required. & exit /b 1)
)
"%PY%" -m pip install --disable-pip-version-check -q -r "%REPO%\requirements.txt" pyinstaller==6.22.3 || exit /b 1

"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN --distpath "%OUT%\dist" --workpath "%OUT%\build" "%REPO%\packaging\alfred.spec" || exit /b 1
copy /y "%REPO%\packaging\README-testers.txt" "%OUT%\dist\Alfred\README-testers.txt" >nul || exit /b 1
copy /y "%REPO%\packaging\Uninstall-Alfred.cmd" "%OUT%\dist\Alfred\Uninstall-Alfred.cmd" >nul || exit /b 1
copy /y "%REPO%\THIRD-PARTY-NOTICES.md" "%OUT%\dist\Alfred\THIRD-PARTY-NOTICES.md" >nul || exit /b 1
rem Search by meaning works out of the box: the pinned llama.cpp engine and the search model go next to Alfred.exe
rem (downloaded once into %OUT%\cache and checked by SHA-256 on every build).
"%PY%" "%REPO%\packaging\bundle_models.py" "%OUT%\dist\Alfred" "%OUT%\cache" || exit /b 1

set "VERSION="
for /f "tokens=2 delims='" %%V in ('findstr /b /c:"__version__" "%REPO%\alfred\__init__.py"') do set "VERSION=%%V"
if not defined VERSION (echo Could not read __version__ from alfred\__init__.py & exit /b 1)
"%PY%" "%REPO%\packaging\make_zip.py" "%OUT%\dist\Alfred" "%OUT%\Alfred-%VERSION%-win64.zip" || exit /b 1
echo.
echo Built %OUT%\dist\Alfred\Alfred.exe
echo Check it without opening a window:  "%OUT%\dist\Alfred\Alfred.exe" --selftest --selftest-log "%OUT%\selftest.log"
