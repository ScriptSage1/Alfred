"""Open a file at a spot: a PDF at a page, a video at a moment, when the default app can do it.

The default app for the file type is looked up read-only (AssocQueryStringW; nothing is written to the
registry). Apps that take a "start here" switch get it; every other app, and any failure, opens the file
the plain way. ``command_for`` builds the command line as an argument list (no shell) and launches nothing.
"""
import os
import subprocess
import sys
from pathlib import Path

BROWSERS = {'msedge.exe', 'chrome.exe', 'firefox.exe', 'brave.exe'}
ACROBAT = {'acrobat.exe', 'acrord32.exe', 'acrobat reader.exe'}
VIDEO_PLAYERS = {'vlc.exe', 'mpv.exe'}
PDF = '.pdf'
VIDEO = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.wmv', '.mpeg', '.mpg'}


def _whole(value, minimum):
    """``value`` as an int when it is a plain number (not a bool) of at least ``minimum``, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return int(value) if value >= minimum else None


def command_for(handler_exe, path, loc):
    """The argument list that opens ``path`` at the place ``loc`` names ('page' or 'seconds') in the app
    ``handler_exe``, or None when that app can't be told where to start (open the file the plain way)."""
    if not handler_exe or not path or not isinstance(loc, dict) or not os.path.isabs(str(path)):
        return None
    app, suffix, path = Path(str(handler_exe)).name.lower(), Path(str(path)).suffix.lower(), str(path)
    page, seconds = _whole(loc.get('page'), 1), _whole(loc.get('seconds'), 0)
    if suffix == PDF and page:
        if app in BROWSERS:
            return [str(handler_exe), f'{Path(path).as_uri()}#page={page}']  # the URI escapes spaces, '#' and non-ASCII
        if app == 'sumatrapdf.exe':
            return [str(handler_exe), '-page', str(page), path]
        if app in ACROBAT:
            return [str(handler_exe), '/A', f'page={page}', path]
    if suffix in VIDEO and seconds is not None:
        if app == 'vlc.exe':
            return [str(handler_exe), f'--start-time={seconds}', path]
        if app == 'mpv.exe':
            return [str(handler_exe), f'--start={seconds}', path]
    return None


def default_handler(path):
    """The executable Windows opens this file type with, or None (read-only lookup)."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        query = ctypes.windll.shlwapi.AssocQueryStringW
        query.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        # ASSOCF_NOTRUNCATE | ASSOCF_REMAPRUNDLL, ASSOCSTR_EXECUTABLE
        if query(0x20 | 0x80, 2, Path(path).suffix.lower(), 'open', buffer, ctypes.byref(size)) != 0:
            return None
        return buffer.value if buffer.value and os.path.isfile(buffer.value) else None
    except Exception:
        return None


def open_at(path, loc=None, plain=None):
    """Open ``path`` (already checked to be inside a tracked folder) at ``loc`` when its default app supports
    that; otherwise with ``plain`` (default: the system's normal open)."""
    plain = plain or _plain
    command = command_for(default_handler(path), path, loc) if loc else None
    if command:
        try:
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            subprocess.Popen(command, close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    plain(path)
    return False


def _plain(path):
    if os.name == 'nt':
        os.startfile(str(path))
    else:
        subprocess.Popen(['xdg-open' if sys.platform != 'darwin' else 'open', str(path)])
