"""Start Alfred when the user signs in to Windows: a shortcut in the Startup folder.

A shortcut (rather than a Run registry value) can set the working directory and
needs no administrator rights. For a clone it launches the repo's .venv pythonw
(no console window) with ``-m alfred --startup``; for a frozen build it launches
Alfred.exe ``--startup``. Either way Alfred starts hidden in the tray.

``TASKS`` lists the components started at sign-in (Ollama, the index catch-up; later scripts).
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import REPO

NAME = 'Alfred.lnk'
# Windows identifies Alfred's notifications by this ID; register_app() gives it the name "Alfred".
APP_ID = 'Alfred.Desktop'
ASSETS = Path(__file__).resolve().parent / 'assets'
FROZEN = getattr(sys, 'frozen', False)
TASK_DELAY_MS = 15000  # give Windows and drivers a moment after sign-in before starting extra components


def start_ollama(window):
    """Start Ollama hidden if it isn't running and load the search model (runs in the background)."""
    window.warm_up()


def catch_up_index(window):
    """Index what changed in the tracked folders while Alfred wasn't running."""
    window.start_index(quiet=True, auto=True)


# Callables run once, in order, after TASK_DELAY_MS when Alfred starts at sign-in. Each gets the
# AlfredWindow and must return quickly (start background work, don't block); a task that raises
# is logged and skipped. Add more components here.
TASKS = [start_ollama, catch_up_index]


def run_tasks(window):
    for task in TASKS:
        try:
            task(window)
        except Exception:
            logging.getLogger('alfred').exception('Startup task %r failed', task)


def sandboxed():
    """True when Windows redirects this process's AppData writes into another app's package
    (e.g. Alfred was started from inside a packaged app). Settings and the index would then
    land in that app's private folder, so Alfred restarts itself normally instead."""
    if os.name != 'nt':
        return False
    import tempfile
    base = os.environ.get('LOCALAPPDATA')
    if not base or not os.path.isdir(base):
        return False
    try:
        handle, probe = tempfile.mkstemp(prefix='.alfred-probe-', dir=base)
        os.close(handle)
    except OSError:
        return False
    try:
        real = os.path.realpath(probe)
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return os.path.normcase(real) != os.path.normcase(os.path.abspath(probe)) and os.sep + 'packages' + os.sep in os.path.normcase(real)


def relaunch_outside():
    """Ask Windows Explorer to start Alfred (Explorer runs it outside any package). True on success."""
    target = Path(sys.executable) if FROZEN else REPO / 'Alfred.cmd'
    if not target.is_file():
        return False
    try:
        subprocess.Popen(['explorer.exe', str(target)], close_fds=True)
        logging.getLogger('alfred').info('Started from a redirected (packaged) context; relaunched through Explorer')
        return True
    except OSError:
        return False


def register_app():
    """Tell Windows Alfred's display name, so notifications say "Alfred" (not the raw app ID).
    Per-user (HKCU), no administrator rights; rewritten each start so it follows the install folder."""
    if os.name != 'nt':
        return
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'Software\Classes\AppUserModelId\{APP_ID}') as key:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, 'Alfred')
            # No IconUri: the notification already shows the large logo, so the header stays text-only.
            for name in ('IconUri', 'IconBackgroundColor'):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except OSError as error:
        logging.getLogger('alfred').warning('Could not register Alfred with Windows notifications: %s', error)

    def refresh_shortcut():  # keep the sign-in shortcut's icon current (in the background: it runs PowerShell)
        try:
            if (startup_folder() / NAME).is_file() and enabled():
                enable()
        except (OSError, subprocess.SubprocessError):
            pass
    import threading
    threading.Thread(target=refresh_shortcut, daemon=True).start()


def startup_folder():
    return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'


def pythonw():
    venv = REPO / '.venv' / 'Scripts' / 'pythonw.exe'
    if venv.is_file():
        return venv
    candidate = Path(sys.executable).with_name('pythonw.exe')
    return candidate if candidate.is_file() else Path(sys.executable)


def command():
    """(target, arguments) the shortcut launches."""
    if FROZEN:
        return Path(sys.executable), '--startup'
    return pythonw(), '-m alfred --startup'


def _powershell(script):
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True,
                          timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _quote(value):
    return str(value).replace("'", "''")


def enabled(folder=None):
    """True when the Startup shortcut exists and points at this Alfred."""
    link = Path(folder or startup_folder()) / NAME
    if not link.is_file():
        return False
    out = _powershell(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{_quote(link)}'); $s.TargetPath + '|' + $s.WorkingDirectory")
    target, _, workdir = out.stdout.strip().partition('|')
    return bool(target) and Path(target).resolve() == command()[0].resolve() and Path(workdir or '.').resolve() == REPO.resolve()


def enable(folder=None):
    folder = Path(folder or startup_folder())
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / NAME
    target, arguments = command()
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{_quote(link)}');"
              f"$s.TargetPath='{_quote(target)}'; $s.Arguments='{arguments}'; $s.WorkingDirectory='{_quote(REPO)}';"
              f"$s.IconLocation='{_quote(ASSETS / 'alfred.ico')},0';"
              "$s.WindowStyle=7; $s.Description='Alfred (starts hidden in the tray)'; $s.Save()")
    out = _powershell(script)
    if out.returncode or not link.is_file():
        raise OSError(f'Could not create the Startup shortcut: {(out.stderr or out.stdout).strip()[:200]}')
    return link


def disable(folder=None):
    link = Path(folder or startup_folder()) / NAME
    if link.is_file():
        link.unlink()


def first_run(settings):
    """Turn on start-at-sign-in once; afterwards only the Settings switch changes it."""
    if settings.get('start_with_windows_configured'):
        return
    try:
        enable()
        logging.getLogger('alfred').info('Start with Windows enabled (first run)')
    except OSError as error:
        logging.getLogger('alfred').warning('Could not enable start with Windows: %s', error)
    settings['start_with_windows_configured'] = True
    settings.save()
