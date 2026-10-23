"""Path safety shared by indexing, watching and the UI.

Alfred only reads files inside folders the user tracks. Symlinks, junctions,
hidden/system entries, dot-folders and files that look like credentials are
never indexed, and folders that would expose the whole system (a drive root,
the user profile itself, Windows, Program Files, AppData) cannot be tracked.
"""
import os
import re
from pathlib import Path

REPARSE = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT (symlinks, junctions)
HIDDEN_SYSTEM = 0x2 | 0x4
SKIP_DIRS = {'node_modules', '__pycache__', '$recycle.bin', 'system volume information', 'appdata'}
SECRET_SUFFIXES = {'.pem', '.key', '.pfx', '.p12', '.kdbx', '.keychain', '.ovpn', '.gpg', '.asc', '.jks', '.keystore', '.ppk'}
SECRET_NAMES = re.compile(r'(^id_(rsa|dsa|ecdsa|ed25519)\b)|(^\.?env(\..*)?$)|(^\.(netrc|npmrc|pypirc|git-credentials)$)'
                          r'|password|passwd|secret|credential|private[_-]?key|api[_-]?key|token', re.I)


class PathError(ValueError):
    """A path is outside the tracked folders or otherwise unsafe."""


def canonical(path):
    """Resolve symlinks/junctions and normalise case/separators for comparison."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_within(root, path):
    root_c, path_c = canonical(root), canonical(path)
    return path_c == root_c or path_c.startswith(root_c.rstrip(os.sep) + os.sep)


def resolve_within(root, candidate, must_exist=True):
    """Real path of ``candidate`` (relative, or absolute inside ``root``) or PathError."""
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 2048 or '\x00' in candidate:
        raise PathError('Invalid path')
    target = Path(os.path.realpath(Path(root) / candidate))
    if not is_within(root, target):
        raise PathError('Path is outside the permitted folder')
    if must_exist and not target.exists():
        raise PathError('File not found')
    return target


def relative_to_root(root, path):
    return Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root))).as_posix()


def looks_secret(name):
    """True for file names that typically hold keys, passwords or tokens."""
    lowered = name.lower()
    return Path(lowered).suffix in SECRET_SUFFIXES or bool(SECRET_NAMES.search(lowered))


def skip_dir(name):
    return name.startswith('.') or name.lower() in SKIP_DIRS


def _known_folder(name):
    path = {'windows': os.environ.get('SystemRoot') or os.environ.get('WINDIR'),
            'program_files': os.environ.get('ProgramFiles'), 'program_files_x86': os.environ.get('ProgramFiles(x86)'),
            'program_data': os.environ.get('ProgramData')}.get(name)
    return Path(path) if path else None


def forbidden_reason(folder, data_home=None):
    """Why ``folder`` may not be tracked, or None when it is fine."""
    folder = Path(folder)
    if not folder.is_dir():
        return 'That folder does not exist.'
    real = Path(os.path.realpath(folder))
    if real.parent == real:
        return 'For privacy, Alfred won’t track a whole drive. Choose a folder such as Downloads or Documents.'
    home = Path(os.path.realpath(Path.home()))
    if canonical(real) == canonical(home):
        return 'For privacy, Alfred won’t track your whole user folder. Choose Downloads, Documents, Pictures or another folder.'
    blocked = [home / 'AppData'] + [p for p in (_known_folder(n) for n in ('windows', 'program_files', 'program_files_x86', 'program_data')) if p]
    if data_home:
        blocked.append(Path(data_home))
    for place in blocked:
        if is_within(place, real):
            return f'Alfred can’t track system or app folders ({place.name}). Choose a folder with your own files.'
    return None


def shorten(path, width=48):
    """C:\\Users\\me\\Downloads\\Reports → ~\\Downloads\\Reports, ellipsised in the middle if still long."""
    text = str(path)
    home = str(Path.home())
    if os.path.normcase(text).startswith(os.path.normcase(home)):
        text = '~' + text[len(home):]
    if len(text) <= width:
        return text
    keep = (width - 1) // 2
    return text[:keep] + '…' + text[-(width - 1 - keep):]
