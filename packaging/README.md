# Packaging Juno as a folder build

A PyInstaller one-folder build: `Juno\Juno.exe` (windowed, no console) and its `_internal` folder, zipped for
testers who have no Python.

## Build

Double-click `packaging\build.cmd` (or run it from a terminal). It

1. creates a build environment in `%JUNO_BUILD_DIR%\venv` (default `%USERPROFILE%\AlfredDigUpBench\exe_build`),
   never the repository's `.venv`;
2. installs `requirements.txt` and PyInstaller (pinned in the script);
3. runs `juno.spec` with `build\` and `dist\` in that folder (nothing large lands in the repository);
4. copies `README-testers.txt`, `Uninstall-Juno.cmd` and `THIRD-PARTY-NOTICES.md` into `dist\Juno`;
5. runs `bundle_models.py`, which puts what search by meaning needs next to `Juno.exe`, so it works without a
   download: the pinned llama.cpp build in `runtime\llama\` and the EmbeddingGemma 2 text model (304 MB) with its
   Apache-2.0 licence in `models\embeddinggemma-2\`. They are downloaded once into `%JUNO_BUILD_DIR%\cache` and
   checked against the SHA-256 pinned in `juno\models.py` on every build. The picture projector (about 1 GB) is
   not bundled; Settings still offers it as a download;
6. zips the folder to `Juno-<version>-win64.zip` (the version is `__version__` in `juno\__init__.py`). The model
   file is stored uncompressed, since quantised weights barely compress.

Check the result without opening a window (it works in a temporary data folder and removes it afterwards):

    dist\Juno\Juno.exe --selftest --selftest-log selftest.log

The last line of the log is `SELFTEST OK` or `SELFTEST FAILED: ...` (the exit code is 0 or 1). From a clone,
`python -m juno --selftest` runs the same checks.

Files: `juno.spec` (what is collected and excluded), `juno_entry.py` (the frozen entry point; it calls
`multiprocessing.freeze_support()` first, for the PDF-table worker processes), `bundle_models.py`, `make_zip.py`,
`README-testers.txt`. In a frozen build the self-test fails if the engine or the search model is missing.

## What a tester does

1. Unzip anywhere and run `Juno.exe`. Windows SmartScreen warns about an unsigned program: **More info → Run anyway**.
2. Keyword search and search by meaning work at once (the engine and search model come in the folder; Settings shows
   them as "Came with Juno"). Only picture search by content needs a download.
3. Settings, the index, any downloaded models and the log (`assistant.log`) live in `%LOCALAPPDATA%\Juno`.
   Juno adds `Juno.lnk` to the Startup folder on first run (Settings → Start Juno when you sign in).
4. To use Juno from Claude Code: Settings → AI tools shows the command (`Juno.exe --mcp`).
5. To remove it: switch off start at sign-in, quit from the tray, delete the folder and `%LOCALAPPDATA%\Juno`.
