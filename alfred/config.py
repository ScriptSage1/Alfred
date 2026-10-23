"""Single source of truth for Alfred settings.

Settings live in %LOCALAPPDATA%\\Alfred\\settings.json (override with
ALFRED_HOME), outside the repository. API keys may instead come from the
ALFRED_API_KEY / ALFRED_EMBED_API_KEY / ALFRED_ANTHROPIC_API_KEY environment
variables. Secrets are never returned by ``public()``.
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
    # AI generation: 'none' | 'ollama' | 'openai' | 'claude_code' | 'anthropic'. Off until the AI backend is added.
    'provider': 'none',
    'claude_code_model': 'sonnet',
    'claude_code_path': '',
    'base_url': '',
    'model': '',
    'api_key': '',
    'device': 'auto',            # ollama only: 'auto' (GPU when available) | 'cpu'
    'ollama_autostart': True,    # start Ollama (headless) and warm the models when Alfred starts
    'keep_alive_minutes': 5,
    'gpu_layers': -1,
    'temperature': 0.2,
    'effort': 'medium',
    'web_search': True,
    'claude_model': 'claude-sonnet-5-5',
    'anthropic_api_key': '',
    'anthropic_base_url': '',
    'top_k': 6,
    # Semantic search: EmbeddingGemma 2 through a local Ollama. Keyword search always works.
    'embed_enabled': True,
    'embed_provider': 'ollama',
    'embed_base_url': 'http://127.0.0.1:11434',
    'embed_model': 'embeddinggemma-2',
    'embed_api_key': '',
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
    # Obsidian
    'vault_path': '',
    'notes_folder': 'Alfred',
    # UI
    'reduce_motion': False,
    'theme': 'system',           # 'system' | 'dark' | 'light'
    'hotkey': 'Ctrl+Alt+L',      # change in Settings; falls back if another app owns it
    'sidebar_collapsed': False,
    # Start with Windows: turned on once at first launch; after that only the Settings switch changes it.
    'start_with_windows_configured': False,
}
SECRETS = ('api_key', 'embed_api_key', 'anthropic_api_key')
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
        for key, env in (('api_key', 'ALFRED_API_KEY'), ('embed_api_key', 'ALFRED_EMBED_API_KEY'),
                         ('anthropic_api_key', 'ALFRED_ANTHROPIC_API_KEY')):
            if os.environ.get(env):
                self[key] = os.environ[env]

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

    def public(self):
        """Settings safe to show: secrets reduced to a boolean."""
        values = {k: v for k, v in self.items() if k not in SECRETS}
        for key in SECRETS:
            values[key + '_set'] = bool(self.get(key))
        return values


def load(home=None):
    return Settings(home)
