"""Single source of truth for Alfred settings.

Settings live in %LOCALAPPDATA%\\Alfred\\settings.json (override with
ALFRED_HOME), outside the repository. Every model runs locally through Ollama.
"""
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

DEFAULTS = {
    # Folders Alfred indexes and watches (absolute paths), added under Settings → Tracked folders.
    'tracked_folders': [],
    'auto_index': True,          # watch tracked folders and index new/changed files automatically
    # Chat (Ask Alfred): a model running in Ollama on this PC. Empty until you choose one.
    'model': '',
    'base_url': 'http://127.0.0.1:11434',
    'device': 'auto',            # 'auto' (GPU when available) | 'cpu'
    'effort': 'off',             # thinking, for models that support it: off | on (gpt-oss: low | medium | high)
    'temperature': 0.2,
    'top_k': 6,
    'ollama_autostart': True,    # start Ollama (headless) and warm the search model when Alfred starts
    'keep_alive_minutes': 5,
    # Chat history: one JSON file per chat. Empty folder = %LOCALAPPDATA%\Alfred\Chats.
    'save_chat_history': True,
    'chats_folder': '',
    # Semantic search: EmbeddingGemma 2 through a local Ollama. Keyword search always works.
    'embed_enabled': True,
    'embed_provider': 'ollama',
    'embed_base_url': 'http://127.0.0.1:11434',
    'embed_model': 'embeddinggemma-2',
    'embed_device': 'auto',
    'embed_batch': 16,
    # Text in images, scanned PDF pages and video frames, read by a local OCR model.
    'ocr_enabled': True,
    'video_interval': 10,        # seconds between sampled video frames
    # Captions, descriptions and tags for pictures (and one frame per video) from a local Ollama vision model.
    'vision_enabled': True,
    'vision_model': 'gemma3:4b',
    'vision_base_url': '',       # empty = the same Ollama as embeddings
    'vision_device': 'auto',
    # UI
    'reduce_motion': False,
    'theme': 'system',           # 'system' | 'dark' | 'light'
    'hotkey': 'Ctrl+Alt+L',      # change in Settings; falls back if another app owns it
    'sidebar_collapsed': False,
    # Start with Windows: turned on once at first launch; after that only the Settings switch changes it.
    'start_with_windows_configured': False,
}
LEGACY_FILES = ('settings.json', 'alfred.sqlite', 'alfred.sqlite-wal', 'alfred.sqlite-shm', 'claude-code-runs')
_lock = threading.Lock()


def data_home():
    base = os.environ.get('ALFRED_HOME') or os.path.join(os.environ.get('LOCALAPPDATA') or str(Path.home()), 'Alfred')
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def retire_legacy(home):
    """Move an earlier Alfred's settings and index aside (once), so they can't leak into this one."""
    settings = home / 'settings.json'
    try:
        stored = json.loads(settings.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return
    if 'start_with_windows_configured' in stored:
        return
    backup = home / 'old-app-backup'
    if backup.exists():
        backup = home / f'old-app-backup-{time.strftime("%Y%m%d-%H%M%S")}'
    backup.mkdir(parents=True)
    for name in LEGACY_FILES:
        if (home / name).exists():
            shutil.move(str(home / name), str(backup / name))
    logging.getLogger('alfred').info('Moved an earlier Alfred’s settings and index to %s', backup)


class Settings(dict):
    """Dict of settings; ``save()`` persists atomically."""

    def __init__(self, home=None):
        super().__init__(DEFAULTS)
        self['tracked_folders'] = []
        self.home = Path(home) if home else data_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'settings.json'
        retire_legacy(self.home)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8-sig'))
                self.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass

    def save(self):
        stored = {k: v for k, v in self.items() if k in DEFAULTS}
        with _lock:
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(stored, indent=2), encoding='utf-8')
            temp.replace(self.path)

    @property
    def folders(self):
        """Tracked folders as Paths, in the order they were added."""
        return [Path(p) for p in self.get('tracked_folders') or []]

    @property
    def db_path(self):
        return self.home / 'index.sqlite'

    @property
    def chats_folder(self):
        folder = str(self.get('chats_folder') or '').strip()
        return Path(os.path.expandvars(folder)) if folder else self.home / 'Chats'


def load(home=None):
    return Settings(home)
