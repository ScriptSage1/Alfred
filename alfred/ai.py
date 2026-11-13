"""Local models: EmbeddingGemma 2 embeddings for semantic search through the llama.cpp servers Alfred owns
(alfred.runtime), plus the Ollama helpers the answer side still uses (phase B removes them) and diagnostics.

Chat providers (Ollama, Claude, ChatGPT, APIs) live in alfred.providers.
"""
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import runtime
from .console import hidden

OLLAMA_DEFAULT = 'http://127.0.0.1:11434'
DEFAULT_CONTEXT_MODEL = 'gemma3:4b'  # writes passage contexts (and rewrites queries) when no other local model is chosen


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


def _options(device, threads=0):
    """Ollama request options: a model asked to run on the CPU gets no GPU layers and, when the indexing thread
    budget is set (setting index_threads), that many threads."""
    if device != 'cpu':
        return {}
    options = {'num_gpu': 0}
    if threads and int(threads) > 0:
        options['num_thread'] = int(threads)
    return options


def _threads(settings):
    try:
        return max(0, int(settings.get('index_threads') or 0))
    except (TypeError, ValueError):
        return 0


PICTURE_SIDE = 512  # pictures are embedded at this size: larger costs the same and finds nothing more
PICTURE_BATCH = 8


def picture_jpeg(image, side=PICTURE_SIDE):
    """Base64 JPEG (quality 88) of a PIL image, RGB, its longest side at most ``side``."""
    import base64
    import io
    picture = image.convert('RGB')
    if max(picture.size) > side:
        picture.thumbnail((side, side))
    buffer = io.BytesIO()
    picture.save(buffer, 'JPEG', quality=88)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


# -------------------------------------------------------------- embeddings
class Embedder:
    """EmbeddingGemma 2 with its prompt format, on the llama.cpp servers of alfred.runtime: queries and small indexing
    jobs on the search server, larger jobs and every picture on the index server."""
    MODEL = 'embeddinggemma-2'

    def __init__(self, settings):
        self.kind = 'llama'
        self.model = self.MODEL  # the only embedding model: a settings file naming another one gets this one
        self.settings = settings
        self.device = settings.get('embed_device', 'auto')  # the indexing device; search follows search_on_gpu
        self.batch = max(1, min(int(settings.get('embed_batch') or 16), 128))
        self.keep_alive = keep_alive(settings)
        self.family = 'gemma'
        # Cosine similarity below which a meaning-only match is noise (measured on EmbeddingGemma 2:
        # related passages score about 0.70+, unrelated ones 0.58–0.65).
        self.min_similarity = 0.66
        # Similarity at which a meaning match alone is confident enough to show (index.is_confident). Calibrated
        # for EmbeddingGemma 2 on the benchmark's synthetic corpus: the best passage for unrelated queries scored
        # 0.60-0.65, the relevant passage for paraphrase/meaning queries 0.69+.
        self.confident_similarity = 0.67
        # The same for a picture vector (a text query against a picture). Measured on the benchmark's medium corpus
        # with Ollama: the right picture for a query about what it shows scored 0.715-0.858, the best picture for
        # queries with no picture answer at most 0.709. M13 design, section 9: while Ollama-made picture vectors remain,
        # the floor is the higher of this and the floor measured on llama.cpp vectors (not measured yet).
        self.confident_picture_similarity = 0.712
        self.threads = _threads(settings)
        self.role = None  # set by plan() for an indexing job; None: chosen per request by its size

    @classmethod
    def from_settings(cls, settings):
        return cls(settings) if settings.get('embed_enabled') else None

    @property
    def local(self):
        return True

    def available(self):
        """Whether the engine and the search model are on this PC (no server is started)."""
        return runtime.missing(self.settings) is None

    def signature(self):
        """Changes whenever stored vectors would no longer be comparable. It names the vector space, not the engine:
        llama.cpp's vectors match Ollama's (0.9999), so the string stays and nothing is re-embedded."""
        return f'ollama:{self.model}:prompt-v1'

    def document_prompt(self, title, text):
        return f'title: {title or "none"} | text: {text}'

    def query_prompt(self, text):
        return f'task: search result | query: {text}'

    # ------------------------------------------------------------ indexing jobs
    def plan(self, passages):
        """An indexing run is about to embed ``passages`` passages: a small job goes through the search server in small
        batches, a larger one to the index server. Returns the batch size to send."""
        self.role = runtime.role_for_job(passages, settings=self.settings)
        return runtime.small_job(self.settings)[1] if self.role == 'search' else self.batch

    def finish(self):
        """The run's embedding work is done: the index server exits after a 60 s linger unless a new run starts."""
        self.role = None
        runtime.release('index', self.settings)

    # ------------------------------------------------------------------- text
    def _post_texts(self, role, prompts, timeout):
        try:
            result = runtime.call(role, '/v1/embeddings', {'input': prompts}, self.settings, timeout=timeout)
            data = sorted(result['data'], key=lambda d: d['index'])
            vectors = [_unit(d['embedding'][-runtime.EMBED_DIM:]) for d in data]
        except runtime.HttpError:
            raise
        except runtime.ServerError as error:
            raise ProviderError(str(error)) from None
        except (KeyError, TypeError, ValueError):
            raise ProviderError('The search engine returned an unexpected embedding response') from None
        if len(vectors) != len(prompts):
            raise ProviderError('The search engine returned an unexpected embedding response')
        return vectors

    def _texts(self, role, items, timeout):
        """Vectors for [(make_prompt, text)]. llama-server refuses an input longer than it reads (Ollama cut it
        silently): a batch that fails is sent one by one, and an input that is too long is cut to the first half of
        its characters until it is accepted."""
        try:
            return self._post_texts(role, [make(text) for make, text in items], timeout)
        except runtime.HttpError as error:
            if len(items) > 1:
                return [vector for item in items for vector in self._texts(role, [item], timeout)]
            make, text = items[0]
            if _too_long(error) and len(text) > 1:
                return self._texts(role, [(make, text[:len(text) // 2])], timeout)
            raise ProviderError(str(error)) from None

    def embed_documents(self, texts, titles=None):
        titles = titles or [''] * len(texts)
        role = self.role or runtime.role_for_job(len(texts), settings=self.settings)
        items = [((lambda body, title=title: self.document_prompt(title, body)), text[:6000]) for title, text in zip(titles, texts)]
        return self._texts(role, items, runtime.BATCH_TIMEOUT)

    def embed_query(self, text):
        return self._texts('search', [(self.query_prompt, text[:2000])], runtime.QUERY_TIMEOUT)[0]

    # --------------------------------------------------------------- pictures
    # The same model embeds a picture into the space of its text vectors (EmbeddingGemma 2 is multimodal), so a text
    # query finds a picture by what it shows. Only the index server loads the picture projector.
    def embed_pictures(self, images):
        """Vectors for PIL images (or base64 JPEG strings), in batches of PICTURE_BATCH; raises ProviderError."""
        payloads = [image if isinstance(image, str) else picture_jpeg(image) for image in images]
        vectors = []
        for start in range(0, len(payloads), PICTURE_BATCH):
            batch = payloads[start:start + PICTURE_BATCH]
            try:
                result = self._post_pictures(batch)
                data = sorted(result, key=lambda d: d['index'])
                found = [_unit(_flatten(d['embedding'])[-runtime.EMBED_DIM:]) for d in data]
            except runtime.ServerError as error:
                raise ProviderError(str(error)) from None
            except (KeyError, TypeError, ValueError):
                raise ProviderError('The search engine returned an unexpected picture embedding response') from None
            if len(found) != len(batch):
                raise ProviderError('The search engine returned an unexpected picture embedding response')
            vectors += found
        return vectors

    def _post_pictures(self, batch):
        """POST /embeddings with the server's media marker; on HTTP 400 read the marker again and retry once (it is
        random per server start)."""
        for attempt in (0, 1):
            server = runtime.client('index', self.settings, pictures=True)
            if attempt:
                server.refresh_marker()
            payload = {'content': [{'prompt_string': server.marker, 'multimodal_data': [data]} for data in batch]}
            try:
                return runtime.call('index', '/embeddings', payload, self.settings, pictures=True, timeout=runtime.BATCH_TIMEOUT)
            except runtime.HttpError as error:
                if error.status != 400 or attempt:
                    raise

    def supports_pictures(self):
        """Whether pictures can be embedded: the picture projector is on this PC (no server is started)."""
        return runtime.missing(self.settings, pictures=True) is None


def _too_long(error):
    detail = (error.detail or '').lower()
    return error.status in (400, 413, 500) and ('too large' in detail or 'context' in detail or 'exceed' in detail or 'too long' in detail)


def _flatten(embedding):
    """/embeddings gives one row per token for a picture; the picture's vector is the last 768 numbers."""
    if embedding and isinstance(embedding[0], list):
        return [x for row in embedding for x in row]
    return embedding


def _unit(vector):
    """The vector scaled to unit length (vectors are stored and compared at unit length)."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1e-12
    return [x / norm for x in vector]


class Contextualizer:
    """Optional "contextual retrieval": a local Ollama model (setting context_model; DEFAULT_CONTEXT_MODEL when empty)
    writes one or two sentences situating a passage within its document, so a passage like
    "Revenue grew 3%" is found as "ACME's Q2 2026 report: revenue grew 3%". Off unless turned on."""
    PROMPT = ('<document title="{title}">\n{document}\n</document>\n\nHere is a passage from this document:\n<passage>\n{passage}\n</passage>\n\n'
              'Write one or two short sentences that situate this passage within the document (what the document is, and which '
              'part, topic, person, date or project the passage is about), to improve search retrieval of the passage. '
              'Answer with only those sentences.')

    def __init__(self, settings):
        self.model = (settings.get('context_model') or DEFAULT_CONTEXT_MODEL).strip()
        self.base = _ollama_base(settings.get('base_url'))  # the local Ollama of the answer side
        self.device = settings.get('device', 'auto')  # Settings → Ollama → Device, as for local chat models
        self.keep_alive = keep_alive(settings)
        self.threads = _threads(settings)

    @classmethod
    def from_settings(cls, settings):
        return cls(settings) if settings.get('contextual_chunks') else None

    def signature(self):
        return f'ollama:{self.model}:context-v1'

    def situate(self, title, document, passage):
        payload = {'model': self.model, 'prompt': self.PROMPT.format(title=title, document=document[:12000], passage=passage[:3000]),
                   'stream': False, 'keep_alive': self.keep_alive, 'options': dict(_options(self.device, self.threads), temperature=0.0, num_predict=90)}
        try:
            result = _json(self.base + '/api/generate', payload, timeout=180)
        except ProviderError as error:
            if 'HTTP 404' in str(error):
                raise ProviderError(f'The model “{self.model}” is not installed. Run: ollama pull {self.model}') from None
            raise
        return ' '.join((result.get('response') or '').split())[:400]


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
    # In its own process group, so it keeps running after Alfred closes. It gets a console without a window rather than
    # no console at all (DETACHED_PROCESS): the model runners Ollama starts for each model share that hidden console,
    # where without one every runner opened a console window of its own.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    env = dict(os.environ, OLLAMA_HOST=urlparse(base).netloc or '127.0.0.1:11434')
    started = time.time()
    try:
        subprocess.Popen([exe, 'serve'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, env=env, **hidden(flags))
    except OSError as error:
        raise ProviderError(f'Could not start Ollama: {error}') from None
    while time.time() - started < wait:
        if ollama_running(base, timeout=0.5):
            return {'started': True, 'seconds': round(time.time() - started, 1)}
        time.sleep(0.25)
    raise ProviderError(f'Ollama did not start within {int(wait)} s')


def warm_up(settings, progress=lambda text: None):
    """Wake the search server (start it if needed): the 1.3-1.6 s wake overlaps with typing. Raises ProviderError."""
    embedder = Embedder.from_settings(settings)
    report = {'started': False, 'loaded': [], 'keep_alive': keep_alive(settings)}
    if embedder is None:
        return report
    progress('Waking the search model…')
    try:
        report['started'] = runtime.wake('search', settings)
    except runtime.ServerError as error:
        raise ProviderError(str(error)) from None
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
                             capture_output=True, text=True, timeout=10, **hidden())
        gpus = []
        for line in out.stdout.strip().splitlines():
            name, total, used, driver = [part.strip() for part in line.split(',')]
            gpus.append({'name': name, 'vram_total_mib': int(total), 'vram_used_mib': int(used), 'driver': driver})
        return {'available': bool(gpus), 'gpus': gpus, 'note': '' if gpus else 'nvidia-smi reported no GPUs'}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'gpus': [], 'note': f'nvidia-smi failed: {error}'}


_gpu_present = None


def has_discrete_gpu():
    """Whether a discrete (NVIDIA) GPU is present: detect_gpus, asked once per process."""
    global _gpu_present
    if _gpu_present is None:
        try:
            _gpu_present = bool(detect_gpus().get('available'))
        except Exception:
            _gpu_present = False
    return _gpu_present


def diagnostics(settings, probe=False):
    embedder = Embedder.from_settings(settings)
    report = {'checked_at': time.strftime('%H:%M:%S'), 'gpu': detect_gpus(), 'chat': ChatProvider(settings).describe(),
              'embeddings': {'enabled': embedder is not None}, 'errors': []}
    if (settings.get('provider') or 'ollama') == 'ollama':  # the answer side (phase B replaces this)
        report['ollama'] = ollama_runtime(settings.get('base_url'))
    if embedder is None:
        return report
    report['embeddings'].update(provider='llama.cpp', model=embedder.model, location='local')
    reason = runtime.missing(settings)
    if reason:
        report['errors'].append(reason)
    if probe and not reason:
        try:
            embedder.embed_query('diagnostic probe')
        except ProviderError as error:
            report['errors'].append(f'Embedding test failed: {error}')
    servers = runtime.status(settings)
    if servers:
        report['embeddings']['execution'] = '; '.join(_describe_server(s) for s in servers)
        report['embeddings']['gpu_active'] = any(s['device'] != 'cpu' for s in servers)
    else:
        report['embeddings']['execution'] = 'Not running right now (started when needed).'
        report['embeddings']['gpu_active'] = None
    report['embeddings']['servers'] = servers
    return report


def _describe_server(server):
    where = 'the CPU' if server['device'] == 'cpu' else server['device']
    text = f"{server['role']} server on {where}"
    if server['mmproj']:
        text += ' with the picture projector'
    if server['memory_mib'] is not None:
        text += f" ({server['memory_mib']} MB in use)"
    return text


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
