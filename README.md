# Alfred

A Spotlight-style assistant for Windows. Press **Ctrl+Alt+L** anywhere to open it; **Esc** hides it.

## Install (from a clone)

1. Install Python 3.11 or newer.
2. Double-click `Setup-Alfred.cmd`. It creates `.venv`, installs the requirements and starts Alfred.

On its first launch Alfred adds itself to your Windows Startup folder, so from then on it starts
hidden in the tray when you sign in. No administrator rights are needed. Turn this off or back on
under **Settings → Start Alfred when you sign in**; once you turn it off, Alfred leaves it off.

Start Alfred by hand any time with `Alfred.cmd` (or `python -m alfred`). Starting it again while it
runs just opens the panel.

## Shortcut

The default is **Ctrl+Alt+L**. If another app already uses it, Alfred falls back to Ctrl+Alt+K, then
Ctrl+Alt+A, and tells you which one it picked. Change it in **Settings**. Without a free shortcut
Alfred still opens from its tray icon.

## Status

Only the UI is in place. `alfred/ai.py`, `index.py`, `rag.py`, `knowledge.py` and `watcher.py` are
placeholders that report "not set up yet"; replace them with the real modules as they are built.
Extra components that should start at sign-in (Ollama, scripts) go in `TASKS` in `alfred/startup.py`.

Settings and logs live in `%LOCALAPPDATA%\Alfred` (override with `ALFRED_HOME`).
