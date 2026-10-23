"""Root-confinement checks shared by indexing and the UI."""
import os
from pathlib import Path, PureWindowsPath


class PathError(ValueError):
    """A path is outside the permitted root or otherwise unsafe."""


def canonical(path):
    """Resolve symlinks/junctions and normalise case/separators for comparison."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_within(root, path):
    root_c, path_c = canonical(root), canonical(path)
    return path_c == root_c or path_c.startswith(root_c.rstrip(os.sep) + os.sep)


def resolve_within(root, candidate, must_exist=True):
    """Return the real path of ``candidate`` inside ``root`` or raise PathError.

    ``candidate`` must be relative. Absolute paths, drive-qualified paths, UNC
    paths, ``..`` components and anything resolving (through a symlink or
    junction) outside ``root`` are rejected.
    """
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 1024:
        raise PathError('Provide a relative path inside the permitted folder')
    if '\x00' in candidate:
        raise PathError('Invalid character in path')
    pure = PureWindowsPath(candidate.replace('/', '\\'))
    if pure.is_absolute() or pure.drive or pure.root or candidate.startswith(('\\\\', '//')):
        raise PathError('Absolute paths are not accepted; use a path relative to the permitted folder')
    if '..' in pure.parts:
        raise PathError('Path traversal is not allowed')
    root_real = Path(os.path.realpath(root))
    if not root_real.is_dir():
        raise PathError('The permitted folder does not exist')
    target = Path(os.path.realpath(root_real.joinpath(*pure.parts)))
    if not is_within(root_real, target):
        raise PathError('Path resolves outside the permitted folder')
    if must_exist and not target.exists():
        raise PathError('File not found inside the permitted folder')
    return target


def relative_to_root(root, path):
    return Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root))).as_posix()
