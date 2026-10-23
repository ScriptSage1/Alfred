"""Local models through Ollama: EmbeddingGemma 2 embeddings for semantic search,
plus starting Ollama and reporting which device it runs on.

Chat providers (Ollama, Claude, ChatGPT, APIs) live in alfred.providers.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

OLLAMA_DEFAULT = 'http://127.0.0.1:11434'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class ProviderError(RuntimeError):
    pass


class NotConfigured(ProviderError):
    pass


# ------------------------------------------------------------------- http
def _json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', 'replace')[:300]
        raise ProviderError(f'HTTP {error.code}: {detail}') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ProviderError(f'Cannot reach {urlparse(url).netloc}: {getattr(error, "reason", error)}') from None


def is_local_url(url):
    host = (urlparse(url if '//' in (url or '') else f'http://{url}').hostname or '').lower()
    return host in ('127.0.0.1', 'localhost', '::1')


def _ollama_base(url):
    return (url or OLLAMA_DEFAULT).rstrip('/').removesuffix('/api')


def keep_alive(settings):
    """How long Ollama keeps a model loaded after its last use (never under 5 minutes)."""
    try:
        minutes = int(settings.get('keep_alive_minutes', 5))
    except (TypeError, ValueError):
        minutes = 5
    return f'{max(5, min(minutes, 24 * 60))}m'


def _options(device):
    return {'num_gpu': 0} if device == 'cpu' else {}


# -------------------------------------------------------------- embeddings
class Embedder:
    """EmbeddingGemma (or another Ollama embedding model) with its prompt format."""

    def __init__(self, settings):
        self.kind = 'ollama'
        self.model = (settings.get('embed_model') or 'embeddinggemma-2').strip()
        self.base = _ollama_base(settings.get('embed_base_url'))
        self.device = settings.get('embed_device', 'auto')
        self.batch = max(1, min(int(settings.get('embed_batch') or 16), 128))
        self.keep_alive = keep_alive(settings)
        name = self.model.lower()
        self.family = 'gemma' if 'embeddinggemma' in name else 'nomic' if 'nomic' in name else 'plain'
        # Cosine similarity below which a meaning-only match is noise (measured on EmbeddingGemma 2:
        # related passages score about 0.70+, unrelated ones 0.58–0.65).
        self.min_similarity = 0.66 if self.family == 'gemma' else 0.5

    @classmethod
    def from_settings(cls, settings):
        return cls(settings) if settings.get('embed_enabled') and settings.get('embed_model') else None

    @property
    def local(self):
        return is_local_url(self.base)

    def signature(self):
        """Changes whenever stored vectors would no longer be comparable (model or prompt format)."""
        return f'ollama:{self.model}:prompt-v1'

    def document_prompt(self, title, text):
        if self.family == 'gemma':
            return f'title: {title or "none"} | text: {text}'
        if self.family == 'nomic':
            return f'search_document: {title}: {text}'
        return f'{title}: {text}' if title else text

    def query_prompt(self, text):
        if self.family == 'gemma':
            return f'task: search result | query: {text}'
        if self.family == 'nomic':
            return f'search_query: {text}'
        return text

    def _embed(self, texts):
        payload = {'model': self.model, 'input': texts, 'truncate': True, 'keep_alive': self.keep_alive, 'options': _options(self.device)}
        try:
            result = _json(self.base + '/api/embed', payload, timeout=180)
        except ProviderError as error:
            if 'HTTP 404' in str(error):
                raise ProviderError(f'The embedding model “{self.model}” is not installed. Run: ollama pull {self.model}') from None
            raise
        vectors = result.get('embeddings')
        if not vectors or len(vectors) != len(texts):
            raise ProviderError('Ollama returned an unexpected embedding response')
        return vectors

    def embed_documents(self, texts, titles=None):
        titles = titles or [''] * len(texts)
        try:
            return self._embed([self.document_prompt(title, text[:6000]) for title, text in zip(titles, texts)])
        except ProviderError as error:
            if 'memory' in str(error).lower() and len(texts) > 1:  # out of (V)RAM: smaller batches
                half = len(texts) // 2
                return self.embed_documents(texts[:half], titles[:half]) + self.embed_documents(texts[half:], titles[half:])
            raise

    def embed_query(self, text):
        return self._embed([self.query_prompt(text[:2000])])[0]


class Vision:
    """A local Ollama vision model (gemma3:4b by default) that describes pictures for search.

    It answers in strict JSON (caption, description, tags); text in the picture is left to OCR.
    """
    PROMPT = ('You describe photos and pictures for a private, offline file search. Look at the image and answer in JSON with: '
              '"caption": one short sentence saying what the picture shows; '
              '"description": 2-3 plain sentences about the people (how many, what they are doing), objects, setting, season, '
              'colours and mood; "tags": 3-8 short lowercase keywords (objects, setting, people count, season, colours). '
              'Describe only what is visible. Do not guess names or identities, and do not transcribe text.')
    SCHEMA = {'type': 'object', 'required': ['caption', 'description', 'tags'],
              'properties': {'caption': {'type': 'string'}, 'description': {'type': 'string'},
                             'tags': {'type': 'array', 'items': {'type': 'string'}}}}
    MAX_SIDE = 896  # Gemma 3's vision encoder works at 896×896

    def __init__(self, settings):
        self.model = (settings.get('vision_model') or 'gemma3:4b').strip()
        self.base = _ollama_base(settings.get('vision_base_url') or settings.get('embed_base_url'))
        self.device = settings.get('vision_device', 'auto')
        self.keep_alive = keep_alive(settings)

    @classmethod
    def from_settings(cls, settings):
        return cls(settings) if settings.get('vision_enabled') and settings.get('vision_model') else None

    @property
    def local(self):
        return is_local_url(self.base)

    def signature(self):
        """Changes when stored descriptions should be redone (model or prompt)."""
        return f'ollama:{self.model}:describe-v1'

    @classmethod
    def jpeg(cls, image):
        """Base64 JPEG of a PIL image, bounded to MAX_SIDE."""
        import base64
        import io
        picture = image.convert('RGB')
        picture.thumbnail((cls.MAX_SIDE, cls.MAX_SIDE))
        buffer = io.BytesIO()
        picture.save(buffer, 'JPEG', quality=88)
        return base64.b64encode(buffer.getvalue()).decode('ascii')

    def describe(self, image):
        """{'caption', 'description', 'tags'} for a PIL image; raises ProviderError."""
        payload = {'model': self.model, 'prompt': self.PROMPT, 'images': [self.jpeg(image)], 'stream': False, 'format': self.SCHEMA,
                   'keep_alive': self.keep_alive, 'options': dict(_options(self.device), temperature=0.2, num_predict=400)}
        try:
            result = _json(self.base + '/api/generate', payload, timeout=240)
        except ProviderError as error:
            if 'HTTP 404' in str(error):
                raise ProviderError(f'The vision model “{self.model}” is not installed. Run: ollama pull {self.model}') from None
            raise
        return self.parse(result.get('response', ''))

    @staticmethod
    def parse(text):
        try:
            data = json.loads(text)
        except ValueError:
            raise ProviderError('The vision model did not return a description') from None
        clean = lambda value: ' '.join(str(value or '').split())
        caption, description = clean(data.get('caption')), clean(data.get('description'))
        tags = []
        for tag in data.get('tags') or []:
            tag = clean(tag).lower().strip('#.,')
            if tag and tag not in tags and len(tag) <= 30:
                tags.append(tag)
        if not caption and not description:
            raise ProviderError('The vision model returned an empty description')
        return {'caption': caption or description.split('. ')[0], 'description': description, 'tags': tags[:8]}


# ------------------------------------------------------------------ ollama
def find_ollama():
    found = shutil.which('ollama')
    if found:
        return found
    for candidate in (Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe',
                      Path(os.environ.get('ProgramFiles', '')) / 'Ollama' / 'ollama.exe'):
        if candidate.is_file():
            return str(candidate)
    return None


def ollama_running(base=OLLAMA_DEFAULT, timeout=0.8):
    try:
        with urllib.request.urlopen(_ollama_base(base) + '/api/version', timeout=timeout) as response:
            return bool(json.load(response).get('version'))
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError):
        return False


def ensure_ollama(base=OLLAMA_DEFAULT, wait=25.0):
    """Start ``ollama serve`` hidden in the background if it isn't running (local URLs only).
    Returns {'started': bool, 'seconds': float}; raises ProviderError when it can't."""
    base = _ollama_base(base)
    if not is_local_url(base) or ollama_running(base):
        return {'started': False, 'seconds': 0.0}
    exe = find_ollama()
    if not exe:
        raise ProviderError('Ollama is not installed (https://ollama.com) — searching by keyword only.')
    # Detached and windowless; the server keeps running after Alfred closes.
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    env = dict(os.environ, OLLAMA_HOST=urlparse(base).netloc or '127.0.0.1:11434')
    started = time.time()
    try:
        subprocess.Popen([exe, 'serve'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True, env=env)
    except OSError as error:
        raise ProviderError(f'Could not start Ollama: {error}') from None
    while time.time() - started < wait:
        if ollama_running(base, timeout=0.5):
            return {'started': True, 'seconds': round(time.time() - started, 1)}
        time.sleep(0.25)
    raise ProviderError(f'Ollama did not start within {int(wait)} s')


def warm_up(settings, progress=lambda text: None):
    """Make sure Ollama runs and the embedding model is loaded (restarts its keep-alive timer)."""
    embedder = Embedder.from_settings(settings)
    report = {'started': False, 'loaded': [], 'keep_alive': keep_alive(settings)}
    if embedder is None or not embedder.local:
        return report
    if not ollama_running(embedder.base):
        progress('Starting Ollama…')
        report['started'] = ensure_ollama(embedder.base)['started']
    progress('Loading the search model…')
    _json(embedder.base + '/api/embed', {'model': embedder.model, 'input': 'warm up', 'keep_alive': embedder.keep_alive,
                                         'options': _options(embedder.device)}, timeout=180)
    report['loaded'].append(embedder.model)
    return report


def ollama_runtime(base=OLLAMA_DEFAULT):
    """Version and the models Ollama has loaded, with how much of each sits in VRAM."""
    base = _ollama_base(base)
    try:
        version = _json(base + '/api/version', timeout=2).get('version')
        loaded = []
        for model in _json(base + '/api/ps', timeout=3).get('models', []):
            size, vram = model.get('size') or 0, model.get('size_vram') or 0
            share = round(100 * vram / size) if size else 0
            loaded.append({'model': model.get('name', ''), 'size_mib': size // 2 ** 20, 'vram_mib': vram // 2 ** 20, 'gpu_share_percent': share,
                           'device': 'GPU' if share >= 99 else f'{share}% GPU / {100 - share}% CPU' if share else 'CPU'})
        return {'running': True, 'version': version, 'loaded': loaded}
    except ProviderError as error:
        return {'running': False, 'error': f'Ollama is not running ({error})', 'loaded': []}


def detect_gpus():
    exe = shutil.which('nvidia-smi')
    if not exe:
        return {'available': False, 'gpus': [], 'note': 'no NVIDIA driver found (nvidia-smi missing)'}
    try:
        out = subprocess.run([exe, '--query-gpu=name,memory.total,memory.used,driver_version', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        gpus = []
        for line in out.stdout.strip().splitlines():
            name, total, used, driver = [part.strip() for part in line.split(',')]
            gpus.append({'name': name, 'vram_total_mib': int(total), 'vram_used_mib': int(used), 'driver': driver})
        return {'available': bool(gpus), 'gpus': gpus, 'note': '' if gpus else 'nvidia-smi reported no GPUs'}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'gpus': [], 'note': f'nvidia-smi failed: {error}'}


def diagnostics(settings, probe=False):
    embedder, vision = Embedder.from_settings(settings), Vision.from_settings(settings)
    report = {'checked_at': time.strftime('%H:%M:%S'), 'gpu': detect_gpus(), 'chat': ChatProvider(settings).describe(),
              'embeddings': {'enabled': embedder is not None}, 'vision': {'enabled': vision is not None}, 'errors': []}
    models = [(key, model) for key, model in (('embeddings', embedder), ('vision', vision)) if model is not None]
    if not models:
        return report
    for key, model in models:
        report[key].update(provider='ollama', model=model.model, location='local' if model.local else 'remote')
    if probe and embedder is not None:
        try:
            embedder.embed_query('diagnostic probe')
        except ProviderError as error:
            report['errors'].append(f'Embedding test failed: {error}')
    runtime = ollama_runtime(models[0][1].base)
    report['ollama'] = runtime
    if not runtime['running']:
        report['errors'].append(runtime['error'])
    for key, model in models:
        match = next((m for m in runtime['loaded'] if m['model'].split(':')[0] == model.model.split(':')[0]), None)
        if match:
            report[key]['execution'] = f"{match['device']} — {match['vram_mib']} MiB of {match['size_mib']} MiB in VRAM (reported by Ollama)"
            report[key]['gpu_active'] = match['gpu_share_percent'] > 0
        else:
            report[key]['execution'] = 'Not loaded right now (Ollama loads it when needed).'
            report[key]['gpu_active'] = None
    return report


# ------------------------------------------------------------------- chat
_capabilities = {}


def model_capabilities(base, model):
    """Ollama's capability list for a model ('completion', 'vision', 'thinking', 'embedding', ...), cached."""
    key = (_ollama_base(base), model)
    if key not in _capabilities:
        try:
            _capabilities[key] = set(_json(key[0] + '/api/show', {'model': model}, timeout=5).get('capabilities') or [])
        except ProviderError:
            return set()
    return _capabilities[key]


def available_models(settings):
    """Installed Ollama models that can chat: [{'model', 'label', 'vision', 'thinking', 'size_gb'}]."""
    base = _ollama_base(settings.get('base_url'))
    try:
        tags = _json(base + '/api/tags', timeout=3).get('models', [])
    except ProviderError:
        return []
    models = []
    for item in tags:
        name = item['name'].removesuffix(':latest')
        caps = model_capabilities(base, item['name'])
        if 'embed' in name.lower() or (caps and 'completion' not in caps):
            continue  # embedding models can't answer questions
        models.append({'provider': 'ollama', 'model': name, 'vision': 'vision' in caps, 'thinking': 'thinking' in caps,
                       'size_gb': round((item.get('size') or 0) / 1e9, 1), 'label': name})
    return sorted(models, key=lambda m: m['model'])


def ChatProvider(settings):
    """The chat provider chosen in settings (see alfred.providers)."""
    from .providers import for_settings
    return for_settings(settings)
