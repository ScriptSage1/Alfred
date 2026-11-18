"""Start Juno when the user signs in to Windows: a shortcut in the Startup folder.

A shortcut (rather than a Run registry value) can set the working directory and
needs no administrator rights. For a clone it launches the repo's .venv pythonw
(no console window) with ``-m juno --startup``; for a frozen build it launches
Juno.exe ``--startup``. Either way Juno starts hidden in the tray.

``TASKS`` lists the components started at sign-in (the index catch-up; later scripts).
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import REPO, shortcut

NAME = 'Juno.lnk'
# Windows identifies Juno's notifications by this ID; register_app() gives it the name "Juno".
APP_ID = 'Juno.Desktop'
# Juno was called Alfred until November 2026: move_from_alfred swaps these for Juno's.
OLD_NAME, OLD_APP_ID = 'Alfred.lnk', 'Alfred.Desktop'
ASSETS = Path(__file__).resolve().parent / 'assets'
FROZEN = getattr(sys, 'frozen', False)
TASK_DELAY_MS = 15000  # give Windows and drivers a moment after sign-in before starting extra components


def catch_up_index(window):
    """Index what changed in the tracked folders while Juno wasn't running."""
    window.start_index(quiet=True, auto=True)


# Callables run once, in order, after TASK_DELAY_MS when Juno starts at sign-in. Each gets the
# JunoWindow and must return quickly (start background work, don't block); a task that raises
# is logged and skipped. Add more components here. No model starts at sign-in: the search server starts at the first
# search or indexing run (juno.runtime).
TASKS = [catch_up_index]


def run_tasks(window):
    for task in TASKS:
        try:
            task(window)
        except Exception:
            logging.getLogger('juno').exception('Startup task %r failed', task)


def sandboxed():
    """True when Windows redirects this process's AppData writes into another app's package
    (e.g. Juno was started from inside a packaged app). Settings and the index would then
    land in that app's private folder, so Juno restarts itself normally instead."""
    if os.name != 'nt':
        return False
    import tempfile
    base = os.environ.get('LOCALAPPDATA')
    if not base or not os.path.isdir(base):
        return False
    try:
        handle, probe = tempfile.mkstemp(prefix='.juno-probe-', dir=base)
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
    """Ask Windows Explorer to start Juno. True on success.

    It has to be Explorer: a process started from inside a packaged app inherits that app's package identity, and with
    it the redirected AppData, however it is started; Explorer starts programs outside any package. Explorer opens the
    Start menu shortcut (pythonw, so no console window), or Juno.exe for a frozen build."""
    if FROZEN:
        target = Path(sys.executable)
    else:
        try:
            target = add_to_start_menu()
        except (OSError, subprocess.SubprocessError) as error:
            logging.getLogger('juno').warning('Could not create the Start menu shortcut to restart through: %s', error)
            return False
    try:
        subprocess.Popen(['explorer.exe', str(target)], close_fds=True)
        logging.getLogger('juno').info('Started from a redirected (packaged) context; relaunched through Explorer')
        return True
    except OSError:
        return False


def register_app():
    """Tell Windows Juno's display name, so notifications say "Juno" (not the raw app ID).
    Per-user (HKCU), no administrator rights; rewritten each start so it follows the install folder."""
    if os.name != 'nt':
        return
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'Software\Classes\AppUserModelId\{APP_ID}') as key:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, 'Juno')
            # No IconUri: the notification already shows the large logo, so the header stays text-only.
            for name in ('IconUri', 'IconBackgroundColor'):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except OSError as error:
        logging.getLogger('juno').warning('Could not register Juno with Windows notifications: %s', error)

    def refresh_shortcut():  # keep the sign-in shortcut current (in the background, off the UI thread)
        try:
            refresh()
        except OSError:
            pass
    import threading
    threading.Thread(target=refresh_shortcut, daemon=True).start()


def move_from_alfred(startup=None, start_menu=None):
    """Juno was called Alfred until November 2026. An Alfred.lnk in the Startup folder or the Start menu that belongs to
    this install (its working folder is this one) is replaced by Juno.lnk, so start-at-sign-in and the Start menu entry
    stay as the user had them; Alfred's notification registration is removed. Returns the shortcuts replaced."""
    replaced = []
    for folder, make in ((Path(startup or startup_folder()), enable), (Path(start_menu or start_menu_folder()), add_to_start_menu)):
        old = folder / OLD_NAME
        try:
            if not old.is_file() or not _same(shortcut.read(old)['workdir'] or '.', REPO):
                continue
            make(folder)
            old.unlink()
            replaced.append(old)
        except OSError as error:
            logging.getLogger('juno').warning('Could not replace %s: %s', old, error)
    if os.name == 'nt':
        try:
            import winreg
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf'Software\Classes\AppUserModelId\{OLD_APP_ID}')
        except OSError:  # not there (already removed, or never registered)
            pass
    return replaced


def startup_folder():
    return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'


def pythonw():
    venv = REPO / '.venv' / 'Scripts' / 'pythonw.exe'
    if venv.is_file():
        return venv
    candidate = Path(sys.executable).with_name('pythonw.exe')
    return candidate if candidate.is_file() else Path(sys.executable)


def command(at_sign_in=True):
    """(target, arguments) a shortcut launches: at sign-in Juno starts hidden in the tray, otherwise it opens."""
    flag = ' --startup' if at_sign_in else ''
    if FROZEN:
        return Path(sys.executable), flag.strip()
    return pythonw(), '-m juno' + flag


ICON = ASSETS / 'juno.ico'


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _points_here(found):
    """True when a shortcut read with shortcut.read starts this Juno."""
    return _same(found['target'], command()[0]) and _same(found['workdir'] or '.', REPO)


def enabled(folder=None):
    """True when the Startup shortcut exists and points at this Juno."""
    link = Path(folder or startup_folder()) / NAME
    return link.is_file() and _points_here(shortcut.read(link))


def refresh(folder=None):
    """Rewrite the Startup shortcut only when it points at this Juno but is out of date (arguments or icon), so a normal
    start writes nothing to the Startup folder."""
    link = Path(folder or startup_folder()) / NAME
    if not link.is_file():
        return False
    found = shortcut.read(link)
    if not _points_here(found) or (found['arguments'] == command()[1] and _same(found['icon'], ICON)):
        return False
    enable(folder)
    return True


def _shortcut(link, target, arguments, description):
    shortcut.write(link, target, arguments, REPO, ICON, 0, shortcut.SW_SHOWMINNOACTIVE, description)
    if not link.is_file():
        raise OSError(f'Could not create the shortcut {link.name}')
    return link


def enable(folder=None):
    folder = Path(folder or startup_folder())
    folder.mkdir(parents=True, exist_ok=True)
    target, arguments = command()
    return _shortcut(folder / NAME, target, arguments, 'Juno (starts hidden in the tray)')


def start_menu_folder():
    return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'


def add_to_start_menu(folder=None):
    """The Start menu shortcut that opens Juno (the venv's pythonw, so no console window). Returns its path."""
    folder = Path(folder or start_menu_folder())
    folder.mkdir(parents=True, exist_ok=True)
    target, arguments = command(at_sign_in=False)
    return _shortcut(folder / NAME, target, arguments, 'Juno')


def install(settings):
    """Setup-Juno.cmd: add Juno to the Start menu. Starting at sign-in is chosen in Juno's first-run setup (or
    switched on when setup is skipped), never before."""
    return add_to_start_menu()


def disable(folder=None):
    link = Path(folder or startup_folder()) / NAME
    if link.is_file():
        link.unlink()


def first_run(settings):
    """Turn on start-at-sign-in once, for a Juno that tracked folders before the first-run setup existed;
    afterwards only the Settings switch changes it."""
    if settings.get('start_with_windows_configured'):
        return
    try:
        enable()
        logging.getLogger('juno').info('Start with Windows enabled (first run)')
    except OSError as error:
        logging.getLogger('juno').warning('Could not enable start with Windows: %s', error)
    settings['start_with_windows_configured'] = True
    settings.save()
