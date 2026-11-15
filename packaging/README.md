# Packaging Alfred as a folder build

A PyInstaller one-folder build: `Alfred\Alfred.exe` (windowed, no console) and its `_internal` folder, zipped for
testers who have no Python.

## Build

Double-click `packaging\build.cmd` (or run it from a terminal). It

1. creates a build environment in `%ALFRED_BUILD_DIR%\venv` (default `%USERPROFILE%\AlfredDigUpBench\exe_build`),
   never the repository's `.venv`;
2. installs `requirements.txt` and PyInstaller (pinned in the script);
3. runs `alfred.spec` with `build\` and `dist\` in that folder (nothing large lands in the repository);
4. copies `README-testers.txt` into `dist\Alfred` and zips the folder to `Alfred-<version>-win64.zip`
   (the version is `__version__` in `alfred\__init__.py`).

Check the result without opening a window (it works in a temporary data folder and removes it afterwards):

    dist\Alfred\Alfred.exe --selftest --selftest-log selftest.log

The last line of the log is `SELFTEST OK` or `SELFTEST FAILED: ...` (the exit code is 0 or 1). From a clone,
`python -m alfred --selftest` runs the same checks.

Files: `alfred.spec` (what is collected and excluded), `alfred_entry.py` (the frozen entry point; it calls
`multiprocessing.freeze_support()` first, for the PDF-table worker processes), `make_zip.py`, `README-testers.txt`.

## What a tester does

1. Unzip anywhere and run `Alfred.exe`. Windows SmartScreen warns about an unsigned program: **More info → Run anyway**.
2. Keyword search works at once; search by meaning needs the engine and model, which Alfred offers to download
   (about 340 MB) into `%LOCALAPPDATA%\Alfred`.
3. Settings, the index, downloaded models and the log (`assistant.log`) live in `%LOCALAPPDATA%\Alfred`.
   Alfred adds `Alfred.lnk` to the Startup folder on first run (Settings → Start Alfred when you sign in).
4. To use Alfred from Claude Code: Settings → AI tools shows the command (`Alfred.exe --mcp`).
5. To remove it: switch off start at sign-in, quit from the tray, delete the folder and `%LOCALAPPDATA%\Alfred`.
