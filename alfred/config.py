"""Single source of truth for Alfred settings.

Settings live in %LOCALAPPDATA%\\Alfred\\settings.json (override with
ALFRED_HOME), outside the repository. API keys may instead come from the
ALFRED_API_KEY / ALFRED_EMBED_API_KEY / ALFRED_ANTHROPIC_API_KEY environment
variables. Secrets are never returned by ``public()``.
"""
import json
import os
import threading
from pathlib import Path

from . import REPO

DEFAULTS = {
    # Searchable corpus: exactly one folder. Empty = %LOCALAPPDATA%\Alfred\Search; relative paths resolve from the repo.
    'search_root': '',
    'auto_index': True,          # watch the folder and index new/changed files automatically
    # AI generation: 'none' | 'ollama' | 'openai' | 'claude_code' | 'anthropic'. Off until the AI backend is added.
    'provider': 'none',
    'claude_code_model': 'sonnet',
    'claude_code_path': '',
    'base_url': '',
    'model': '',
    'api_key': '',
    'device': 'auto',            # ollama only: 'auto' (GPU when available) | 'cpu'
    'ollama_autostart': True,    # start Ollama (headless) and warm models when Alfred opens
    'keep_alive_minutes': 5,
    'gpu_layers': -1,
    'temperature': 0.2,
    'effort': 'medium',
    'web_search': True,
    'claude_model': 'claude-sonnet-5-5',
    'anthropic_api_key': '',
    'anthropic_base_url': '',
    'top_k': 6,
    # Embeddings for semantic search (optional; keyword search always works).
    'embed_enabled': False,
    'embed_provider': 'ollama',  # 'ollama' | 'openai'
    'embed_base_url': 'http://127.0.0.1:11434',
    'embed_model': 'embeddinggemma-2',
    'embed_api_key': '',
    'embed_device': 'auto',
    'embed_batch': 16,
    # Image understanding with a local vision model.
    'vision_enabled': False,
    'vision_base_url': 'http://127.0.0.1:11434',
    'vision_model': 'gemma3:4b',
    'vision_device': 'auto',
    'video_interval': 10,
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
_lock = threading.Lock()


def data_home():
    base = os.environ.get('ALFRED_HOME') or os.path.join(os.environ.get('LOCALAPPDATA') or str(Path.home()), 'Alfred')
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings(dict):
    """Dict of settings; ``save()`` persists atomically."""

    def __init__(self, home=None):
        super().__init__(DEFAULTS)
        self.home = Path(home) if home else data_home()
        self.path = self.home / 'settings.json'
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
    def root(self):
        raw = str(self.get('search_root') or '').strip()
        if not raw:
            default = self.home / 'Search'
            default.mkdir(parents=True, exist_ok=True)
            return default.resolve()
        configured = Path(os.path.expandvars(raw))
        return (configured if configured.is_absolute() else REPO / configured).resolve()

    @property
    def db_path(self):
        return self.home / 'alfred.sqlite'

    def public(self):
        """Settings safe to show: secrets reduced to a boolean."""
        values = {k: v for k, v in self.items() if k not in SECRETS}
        for key in SECRETS:
            values[key + '_set'] = bool(self.get(key))
        return values


def load(home=None):
    return Settings(home)
