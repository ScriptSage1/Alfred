"""The model files Alfred runs with llama.cpp, and where to find them on this PC.

A file is found, in this order:
  1. Alfred's own copy: ALFRED_HOME\\models\\<folder>\\<name> (checked by SHA-256 when it was downloaded, so here by size).
  2. A development folder, ALFRED_MODELS_DIR: the file under its release name or as a ``sha256-<hash>`` blob.
  3. Ollama's copy: the blob ``sha256-<hash>`` under %OLLAMA_MODELS% or %USERPROFILE%\\.ollama\\models\\blobs.
Copies found in 2 and 3 must have the same size; they are used where they are (never copied, changed or deleted). Their
hash is computed once and remembered by size and modification time in ALFRED_HOME\\models\\hashes.json.
Otherwise the file is missing and the feature that needs it is off until it is downloaded.

Downloads (``download``, ``download_engine``) are resumable (HTTP Range into ``<name>.part``), checked by SHA-256 before
the file is put in place, and show nothing themselves: they report bytes through ``progress`` and stop when ``cancel``
(a threading.Event) is set, keeping the part file for the next try.
"""
import hashlib
import json
import os
import shutil
import threading
import types
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path, PurePosixPath

from . import config


@dataclass(frozen=True)
class ModelFile:
    key: str
    role: str        # what uses it: 'embed' (search and indexing), 'pictures' (the index server only), 'answer'
    label: str
    folder: str      # under ALFRED_HOME\models
    name: str
    size: int
    sha256: str      # also the name of Ollama's blob (sha256-<hash>)
    licence: str
    urls: tuple = ()  # download addresses; empty: Alfred has none for this file yet


# The two EmbeddingGemma 2 files are assets of a release on Alfred's GitHub repository (the user's decision, 2026-11-13).
# Apache 2.0 according to Google's model card; use must follow the Gemma Prohibited Use Policy.
RELEASE = 'https://github.com/ScriptSage1/Alfred/releases/download/models-embeddinggemma-2/'
EMBED_LICENCE = 'Apache-2.0 (Google DeepMind); Gemma Prohibited Use Policy applies'


FILES = {f.key: f for f in (
    ModelFile('embed-text', 'embed', 'EmbeddingGemma 2, text, 8-bit', 'embeddinggemma-2', 'embeddinggemma-2-text-q8_0.gguf',
              303_957_280, '111e84992a0406bacb70d11dddd8874001c783cc56b82c462ec82a2f5b8d08b1',
              EMBED_LICENCE, (RELEASE + 'embeddinggemma-2-text-q8_0.gguf',)),
    ModelFile('embed-pictures', 'pictures', 'EmbeddingGemma 2, picture projector', 'embeddinggemma-2', 'embeddinggemma-2-mmproj.gguf',
              980_895_232, '74bf65c860d292a1eebafd7ca85b44532a2528f03017f9204fe1695916d2c66f',
              EMBED_LICENCE, (RELEASE + 'embeddinggemma-2-mmproj.gguf',)),
    ModelFile('answer', 'answer', 'gemma3:4b', 'gemma3-4b', 'gemma3-4b-q4_k_m.gguf',
              2_499_012_288, 'f06e91e18fc74295d0c781bf91622508c75655e2c18ed869d52447ffe1e667e1', 'Gemma Terms of Use'),
)}

# The llama.cpp build Alfred is tested with (one per Alfred release; see the design, section 8). The zip's hash is pinned
# here; the engine download refuses to run without it. 'unpacked': the server, its libraries and licence files.
BUILD = {'name': 'llama-b12318', 'build': 'b12318', 'variant': 'vulkan-x64', 'label': 'llama.cpp b12318 (Vulkan)',
         'zip': 'llama-b12318-bin-win-vulkan-x64.zip', 'size': 38_052_654, 'unpacked': 94_131_485,
         'sha256': 'bbf0403e8608f8638073842c71a6e8ddeed6bc8aa5d89e4d569994d57ddc8a10', 'licence': 'MIT (llama.cpp)',
         'urls': ('https://github.com/ggml-org/llama.cpp/releases/download/b12318/llama-b12318-bin-win-vulkan-x64.zip',)}
ENGINE = 'engine'           # the key ``state`` and the UI use for the engine
FIRST_RUN = (ENGINE, 'embed-text')  # what search by meaning needs; the first-run question offers these
MARGIN = 200 * 2 ** 20      # free disk space a download leaves on the drive, beyond the file itself
BLOCK = 1 << 20
TIMEOUT = 60

_lock = threading.Lock()


class DownloadError(RuntimeError):
    """A download could not run or did not give the right file."""


class DownloadCancelled(DownloadError):
    """Stopped through ``cancel``; the part file is kept and the next download continues from it."""


def ollama_blob_dirs():
    """Folders where Ollama keeps its blobs, in the order Ollama itself uses them."""
    dirs = []
    if os.environ.get('OLLAMA_MODELS'):
        dirs.append(Path(os.environ['OLLAMA_MODELS']) / 'blobs')
    dirs.append(Path(os.environ.get('USERPROFILE') or Path.home()) / '.ollama' / 'models' / 'blobs')
    return dirs


def own_path(key, home=None):
    file = FILES[key]
    return Path(home or config.data_home()) / 'models' / file.folder / file.name


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(4 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _verified(path, file, home):
    """Whether Ollama's blob at ``path`` really is ``file``: hashed once, remembered by size and modification time."""
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size != file.size:
        return False
    store = Path(home) / 'models' / 'hashes.json'
    entry = str(path)
    with _lock:
        try:
            known = json.loads(store.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            known = {}
        seen = known.get(entry)
        if seen and seen.get('size') == stat.st_size and seen.get('mtime_ns') == stat.st_mtime_ns:
            return seen.get('sha256') == file.sha256
        try:
            digest = _hash_file(path)
        except OSError:
            return False
        known[entry] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest}
        store.parent.mkdir(parents=True, exist_ok=True)
        temp = store.with_suffix(f'.{os.getpid()}.tmp')
        temp.write_text(json.dumps(known, indent=1), encoding='utf-8')
        os.replace(temp, store)
        return digest == file.sha256


def models_dir():
    """ALFRED_MODELS_DIR (development): a folder of model files under their release names or as sha256-<hash> blobs."""
    folder = os.environ.get('ALFRED_MODELS_DIR')
    return Path(folder) if folder else None


def find(key, home=None):
    """Path of a model file (Alfred's own copy, else ALFRED_MODELS_DIR's, else Ollama's), or None when it is not on this PC."""
    file = FILES[key]
    home = Path(home or config.data_home())
    own = own_path(key, home)
    try:
        if own.stat().st_size == file.size:
            return own
    except OSError:
        pass
    folder = models_dir()
    if folder is not None:
        for candidate in (folder / file.name, folder / f'sha256-{file.sha256}'):
            if _verified(candidate, file, home):
                return candidate
    for folder in ollama_blob_dirs():
        blob = folder / f'sha256-{file.sha256}'
        if _verified(blob, file, home):
            return blob
    return None


def source(path, home=None):
    """'alfred' for Alfred's own copy, 'external' for ALFRED_MODELS_DIR's, 'ollama' for Ollama's (Settings says "Using
    Ollama's copy")."""
    path = Path(path)
    if path.is_relative_to(Path(home or config.data_home()) / 'models'):
        return 'alfred'
    folder = models_dir()
    return 'external' if folder is not None and path.is_relative_to(folder) else 'ollama'


# ------------------------------------------------------------------ downloads
def _allowed(url):
    """Only https addresses (plus http://127.0.0.1, which the tests serve)."""
    return url.startswith('https://') or url.startswith('http://127.0.0.1:') or url.startswith('http://127.0.0.1/')


def _free_bytes(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def _mb(size):
    return f'{size / 1e6:,.0f} MB'


def _check_space(target, needed):
    free = _free_bytes(target.parent)
    if free < needed + MARGIN:
        raise DownloadError(f'Not enough free disk space: the download needs {_mb(needed + MARGIN)} '
                            f'(including {_mb(MARGIN)} to spare) and the drive has {_mb(free)}')


def _part(target):
    return target.with_name(target.name + '.part')


def _get(url, part, size, progress, cancel):
    """Fetch ``url`` into ``part``, continuing from the bytes already there. Returns when the part file is complete."""
    done = part.stat().st_size if part.exists() else 0
    if done >= size:
        return
    request = urllib.request.Request(url, headers={'User-Agent': 'Alfred', 'Range': f'bytes={done}-'} if done else {'User-Agent': 'Alfred'})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if not _allowed(response.geturl()):
            raise DownloadError(f'The address led to {response.geturl()}, which is not https')
        if done and response.status == 206:
            first = (response.headers.get('Content-Range') or '').replace('bytes ', '').split('-')[0].strip()
            if first != str(done):
                raise DownloadError('The server sent the wrong part of the file')
            mode = 'ab'
        else:
            done, mode = 0, 'wb'  # a plain 200: the server does not continue downloads, so start again
        with open(part, mode) as out:
            progress(done, size)
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled('Download stopped; it continues from here next time')
                block = response.read(BLOCK)
                if not block:
                    break
                if done + len(block) > size:
                    out.write(block[:size - done])
                    done = size
                    break
                out.write(block)
                done += len(block)
                progress(done, size)
    if done < size:
        raise DownloadError(f'The connection ended after {_mb(done)} of {_mb(size)}')


def _fetch(label, urls, size, sha256, target, progress, cancel, extra=0):
    """Download one file into ``target`` (see the module notes). ``extra``: more disk space needed afterwards (unpacking)."""
    if not sha256:
        raise DownloadError(f'{label} has no pinned checksum, so Alfred does not download it')
    if not urls:
        raise DownloadError(f'{label} has no download address')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = _part(target)
    done = part.stat().st_size if part.exists() else 0
    if done > size:
        part.unlink()
        done = 0
    _check_space(target, size - done + extra)
    errors = []
    for url in urls:
        if not _allowed(url):
            errors.append(f'{url} is not an https address')
            continue
        try:
            _get(url, part, size, progress, cancel)
            break
        except DownloadCancelled:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            errors.append(f'HTTP {error.code} from {url}')
        except (urllib.error.URLError, HTTPException, OSError, DownloadError) as error:
            errors.append(f'{url}: {getattr(error, "reason", error)}')
    else:
        raise DownloadError(f'Could not download {label}: ' + '; '.join(errors))
    if _hash_file(part) != sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(f'{label} did not match its checksum, so it was deleted. Try again later.')
    os.replace(part, target)
    return target


def download(key, home=None, progress=lambda done, total: None, cancel=None):
    """Download model file ``key`` into ALFRED_HOME\\models\\<folder>\\<name>; returns its path."""
    file = FILES[key]
    target = own_path(key, home)
    try:
        if target.stat().st_size == file.size:
            return target
    except OSError:
        pass
    return _fetch(file.label, file.urls, file.size, file.sha256, target, progress, cancel)


def _stop_servers(home, roles):
    from . import runtime  # runtime imports this module
    for role in roles:
        runtime.stop(role, types.SimpleNamespace(home=str(home)))


def _users(key):
    return {'embed': ('search', 'index'), 'pictures': ('index',), 'answer': ('answer',)}[FILES[key].role]


def remove(key, home=None):
    """Delete Alfred's own copy of ``key`` (and its part file), after stopping this process's servers that use it. A copy
    found elsewhere (Ollama's, ALFRED_MODELS_DIR's) is never touched. Returns whether a file was deleted."""
    home = Path(home or config.data_home())
    own = own_path(key, home)
    if not own.exists() and not _part(own).exists():
        return False
    _stop_servers(home, _users(key))
    try:
        own.unlink(missing_ok=True)
        _part(own).unlink(missing_ok=True)
    except PermissionError:
        raise DownloadError(f'{FILES[key].label} is in use by another Alfred program (for example the one AI tools use); '
                            'close it and try again') from None
    return True


# ------------------------------------------------------------------ the engine
def engine_dir(home=None):
    """Where the downloaded engine goes: ALFRED_HOME\\runtime\\llama-<build>."""
    return Path(home or config.data_home()) / 'runtime' / BUILD['name']


def _engine_zip(home):
    return Path(home) / 'runtime' / BUILD['zip']


def _kept(name):
    """Which files of the release zip Alfred keeps: the server, its libraries and the licence files."""
    base = PurePosixPath(name).name.lower()
    return base == 'llama-server.exe' or base.endswith('.dll') or base.startswith(('license', 'licence', 'copying'))


def unpack_engine(archive, final):
    """Unpack the kept files of ``archive`` into ``final``: into a temporary folder first, then renamed into place. A
    path that would leave the folder refuses the whole zip."""
    final = Path(final)
    temp = final.with_name(f'.{final.name}.{os.getpid()}.tmp')
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    root = temp.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = [m for m in bundle.infolist() if not m.is_dir() and _kept(m.filename)]
            for member in members:
                name = member.filename.replace('\\', '/')
                path = (root / name).resolve()
                if PurePosixPath(name).is_absolute() or ':' in name or not path.is_relative_to(root) or path == root:
                    raise DownloadError(f'The engine download holds an unsafe path ({member.filename}); nothing was unpacked')
            for member in members:
                path = (root / member.filename.replace('\\', '/')).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source_file, open(path, 'wb') as out:
                    shutil.copyfileobj(source_file, out, BLOCK)
        if not (temp / 'llama-server.exe').is_file():
            raise DownloadError('The engine download has no llama-server.exe')
        if final.exists():
            shutil.rmtree(final)
        os.replace(temp, final)
    except zipfile.BadZipFile as error:
        raise DownloadError(f'The engine download is not a valid zip: {error}') from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return final


def download_engine(home=None, progress=lambda done, total: None, cancel=None):
    """Download the pinned llama.cpp build and unpack llama-server.exe, its libraries and licence files into
    ALFRED_HOME\\runtime\\llama-<build>; returns that folder."""
    home = Path(home or config.data_home())
    final = engine_dir(home)
    if (final / 'llama-server.exe').is_file():
        return final
    archive = _engine_zip(home)
    if not archive.exists():
        _fetch(BUILD['label'], BUILD['urls'], BUILD['size'], BUILD['sha256'], archive, progress, cancel, extra=BUILD['unpacked'])
    unpack_engine(archive, final)
    archive.unlink(missing_ok=True)
    return final


def remove_engine(home=None):
    """Delete the downloaded engine (never one that came with Alfred or ALFRED_LLAMA_DIR), after stopping this process's
    servers. Returns whether anything was deleted."""
    home = Path(home or config.data_home())
    final, archive = engine_dir(home), _engine_zip(home)
    if not final.exists() and not _part(archive).exists():
        return False
    _stop_servers(home, ('search', 'index', 'answer'))
    try:
        _part(archive).unlink(missing_ok=True)
        if final.exists():
            shutil.rmtree(final)
    except PermissionError:
        raise DownloadError('The engine is in use by another Alfred program (for example the one AI tools use); '
                            'close it and try again') from None
    return True


# ------------------------------------------------------------------ for Settings
def state(key, home=None):
    """Where a file is, for Settings: {'key', 'label', 'size', 'where', 'path', 'part', 'downloadable'}.

    where: 'alfred' (Alfred's own copy), 'ollama' (Ollama's), 'external' (ALFRED_MODELS_DIR; for the engine also
    ALFRED_LLAMA_DIR), 'included' (the engine that came with Alfred), or 'missing'. part: bytes of an unfinished
    download (0 when none). downloadable: whether Alfred has an address (and, for the engine, a pinned checksum)."""
    home = Path(home or config.data_home())
    if key == ENGINE:
        from . import runtime  # runtime imports this module
        found = runtime.program_dir(home)
        if runtime.PROGRAM_OVERRIDE:  # tests: a stand-in for llama-server.exe
            where = 'external'
        elif found is None:
            where = 'missing'
        elif found == engine_dir(home):
            where = 'alfred'
        elif os.environ.get('ALFRED_LLAMA_DIR') and found == Path(os.environ['ALFRED_LLAMA_DIR']):
            where = 'external'
        else:
            where = 'included'
        part = _part(_engine_zip(home))
        return {'key': key, 'label': BUILD['label'], 'size': BUILD['size'], 'where': where, 'path': str(found) if found else None,
                'part': part.stat().st_size if part.exists() else 0, 'downloadable': bool(BUILD['urls'] and BUILD['sha256'])}
    file = FILES[key]
    found = find(key, home)
    part = _part(own_path(key, home))
    return {'key': key, 'label': file.label, 'size': file.size, 'where': source(found, home) if found else 'missing',
            'path': str(found) if found else None, 'part': part.stat().st_size if part.exists() else 0,
            'downloadable': bool(file.urls and file.sha256)}


def first_run_needs(home=None):
    """The keys of FIRST_RUN that are missing and can be downloaded (the first-run question offers these)."""
    needs = []
    for key in FIRST_RUN:
        info = state(key, home)
        if info['where'] == 'missing' and info['downloadable']:
            needs.append(key)
    return needs


def size_of(key):
    return BUILD['size'] if key == ENGINE else FILES[key].size


def free_space(home=None):
    """Free bytes on the drive that holds ALFRED_HOME."""
    return _free_bytes(Path(home or config.data_home()))
