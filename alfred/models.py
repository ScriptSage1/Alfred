"""The model files Alfred runs with llama.cpp, and where to find them on this PC.

A file is found, in this order:
  1. Alfred's own copy: ALFRED_HOME\\models\\<folder>\\<name> (checked by SHA-256 when it was downloaded, so here by size).
  2. Ollama's copy: the blob ``sha256-<hash>`` under %OLLAMA_MODELS% or %USERPROFILE%\\.ollama\\models\\blobs, the same
     size. It is used where it is (never copied, changed or deleted). Its hash is computed once and remembered by size
     and modification time in ALFRED_HOME\\models\\hashes.json.
Otherwise the file is missing and the feature that needs it is off. Downloads come in a later task (M13 task 6).
"""
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

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

# The llama.cpp build Alfred is tested with (one per Alfred release; see the design, section 8). The zip's hash is
# pinned when downloads are added (task 6).
BUILD = {'name': 'llama-b12318', 'build': 'b12318', 'variant': 'vulkan-x64', 'size': 36_300_000, 'sha256': '', 'urls': ()}

_lock = threading.Lock()


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


def find(key, home=None):
    """Path of a model file (Alfred's own copy, else Ollama's), or None when it is not on this PC."""
    file = FILES[key]
    home = Path(home or config.data_home())
    own = own_path(key, home)
    try:
        if own.stat().st_size == file.size:
            return own
    except OSError:
        pass
    for folder in ollama_blob_dirs():
        blob = folder / f'sha256-{file.sha256}'
        if _verified(blob, file, home):
            return blob
    return None


def source(path, home=None):
    """'alfred' for Alfred's own copy, 'ollama' for Ollama's (Settings says "Using Ollama's copy")."""
    return 'alfred' if Path(path).is_relative_to(Path(home or config.data_home()) / 'models') else 'ollama'
