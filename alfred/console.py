"""Starting console programs (ffmpeg, Ollama, PowerShell, the Claude and Codex command-line tools) without a window.

Alfred runs under pythonw, which has no console. On Windows a console program started from it gets a new console
window of its own, shown for as long as it runs (Windows 11 opens it in Windows Terminal), unless it is started
hidden. ``hidden()`` gives the keyword arguments for subprocess that keep it hidden: CREATE_NO_WINDOW gives the
program a console with no window, and the programs it starts in turn (Ollama's model runners, a .cmd shim's node.exe)
share that invisible console instead of opening their own; the SW_HIDE start-up info also covers programs that
create a window themselves.
"""
import os
import subprocess


def hidden(flags=0):
    """subprocess keyword arguments that start a console program invisibly (``flags`` are added creation flags)."""
    if os.name != 'nt':
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {'creationflags': subprocess.CREATE_NO_WINDOW | flags, 'startupinfo': info}
