"""Single source of truth for Alfred settings.

Settings live in %LOCALAPPDATA%\\Alfred\\settings.json (override with
ALFRED_HOME), outside the repository. Local models run on llama.cpp servers Alfred owns (alfred.runtime).
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
    # Chat (Ask Alfred): which provider writes answers (see alfred.providers) and its model. Empty until you choose.
    'provider': 'local',         # local | claude_code | codex | anthropic | openai | compatible
    'model': '',
    'compatible_base_url': '',   # an OpenAI-compatible server, e.g. https://openrouter.ai/api/v1
    'cloud_ok': [],              # cloud providers whose privacy notice you've accepted
    'device': 'auto',            # the answer model's device: 'auto' (GPU when available) | 'cpu'
    'effort': 'off',             # thinking, for models that support it: off | on (gpt-oss: low | medium | high)
    'temperature': 0.2,
    'top_k': 6,
    'keep_alive_minutes': 5,     # a local model stays loaded this long after its last use
    'answer_context': 10240,     # tokens the answer model on this PC reads at once (passages + chat so far + answer)
    # Answers from your files (alfred.rag).
    'answer_beyond_files': True, # the model may add general knowledge, clearly labelled, after what your files say
    'rerank_enabled': True,      # reorder candidate passages with the local reranker once it's downloaded
    'rerank_model': 'bge-reranker-v2-m3',
    'contextual_chunks': False,  # the answer model on this PC writes a line of context for every passage while indexing (slow, better recall)
    # Chat history: one JSON file per chat. Empty folder = %LOCALAPPDATA%\Alfred\Chats.
    'save_chat_history': True,
    'chats_folder': '',
    # Semantic search: EmbeddingGemma 2 on llama.cpp servers Alfred starts and owns (alfred.runtime). Keyword search
    # always works. (embed_provider, embed_base_url and embed_model are gone: an older settings file still loads.)
    'embed_enabled': True,
    'embed_device': 'auto',      # the indexing device: 'auto' (the GPU once its check passed) | 'cpu'
    'search_on_gpu': False,      # search runs on the CPU (--device none -ngl 0 -t 4) unless this is on
    'embed_batch': 16,
    # Text in images, scanned PDF pages and video frames, read by a local OCR model.
    'ocr_enabled': True,
    'ocr_device': 'auto',        # 'auto' (DirectML on a discrete GPU, else CPU) | 'gpu' | 'cpu'
    'video_interval': 10,        # seconds between sampled video frames
    # Pictures, video keyframes and scanned PDF pages are found by what they show (the embedding model's picture vectors).
    'picture_search': True,
    # Indexing
    'index_threads': 0,          # CPU threads indexing may use; 0 = all but two (a quarter in the background)
    'index_on_battery': False,   # background indexing past the text layer also runs on battery
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


def migrate(settings, loaded):
    """Settings files written before Alfred ran its own models (M13). Each old key is read once: save() keeps only the
    keys in DEFAULTS, so it is gone after the next save.
      ollama_context  -> answer_context
      provider ollama -> no model chosen (provider local, model empty): never silently switched to the model on this PC
      ollama_autostart, base_url, context_model, embed_provider, embed_base_url, embed_model: dropped"""
    if 'answer_context' not in loaded and isinstance(loaded.get('ollama_context'), int):
        settings['answer_context'] = loaded['ollama_context']
    if loaded.get('provider') == 'ollama':
        settings['provider'], settings['model'] = 'local', ''


class Settings(dict):
    """Dict of settings; ``save()`` persists atomically."""

    def __init__(self, home=None, retire=True):
        super().__init__(DEFAULTS)
        self['tracked_folders'] = []
        self.home = Path(home) if home else data_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'settings.json'
        if retire:  # the MCP server only reads: it never moves an earlier Alfred's files
            retire_legacy(self.home)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8-sig'))
                self.update({k: v for k, v in loaded.items() if k in DEFAULTS})
                migrate(self, loaded)
            except (OSError, ValueError, AttributeError):
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


def load(home=None, retire=True):
    return Settings(home, retire)
