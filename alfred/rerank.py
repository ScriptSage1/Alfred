"""A local cross-encoder reranker: reads the question together with each candidate passage and
scores how well the passage answers it — far sharper than keyword or embedding similarity alone.

Runs on onnxruntime with the model's own tokenizer (the ``tokenizers`` package): on the graphics card
through DirectML when there is one (Windows installs onnxruntime-directml), otherwise on the CPU, and
back on the CPU if the GPU ever fails. A GPU uses the half-precision copy of the model when it has been
downloaded (faster there, and closer to the original than the 8-bit CPU copy). Models are downloaded
once, only when you choose to, into %LOCALAPPDATA%\\Alfred\\models\\<name>; without one Alfred keeps
the fused keyword + meaning order.
"""
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger('alfred')
HF = 'https://huggingface.co'
MODELS = {
    'bge-reranker-v2-m3': {
        'label': 'BGE Reranker v2 M3', 'repo': 'onnx-community/bge-reranker-v2-m3-ONNX', 'languages': 'multilingual',
        'files': {'model.onnx': 'onnx/model_int8.onnx', 'tokenizer.json': 'tokenizer.json'}, 'size_mb': 588,
        'gpu_files': {'model_fp16.onnx': 'onnx/model_fp16.onnx'}, 'gpu_size_mb': 1140,
        'max_length': 512, 'threshold': 0.02},
    'ms-marco-minilm-l6': {
        'label': 'MS MARCO MiniLM L6', 'repo': 'Xenova/ms-marco-MiniLM-L-6-v2', 'languages': 'English',
        'files': {'model.onnx': 'onnx/model_int8.onnx', 'tokenizer.json': 'tokenizer.json'}, 'size_mb': 24,
        'max_length': 512, 'threshold': 0.02},
}
DEFAULT = 'bge-reranker-v2-m3'
_loaded = {}
_load_lock = threading.Lock()


class RerankError(RuntimeError):
    pass


def spec(name):
    return MODELS.get(name) or MODELS[DEFAULT]


def folder(name):
    return config.data_home() / 'models' / (name if name in MODELS else DEFAULT)


def installed(name):
    return all((folder(name) / local).is_file() for local in spec(name)['files'])


def gpu_available():
    """Whether onnxruntime can use a graphics card here (DirectML on Windows, or a CUDA build)."""
    try:
        import onnxruntime as ort
        return any(p in ort.get_available_providers() for p in ('DmlExecutionProvider', 'CUDAExecutionProvider'))
    except ImportError:
        return False


def gpu_model_installed(name):
    return all((folder(name) / local).is_file() for local in spec(name).get('gpu_files', {})) and bool(spec(name).get('gpu_files'))


def dependencies_ok():
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True
    except ImportError:
        return False


def state(settings):
    """{'name', 'label', 'size_mb', 'installed', 'ready', 'enabled', 'detail'} for Settings."""
    name = settings.get('rerank_model') or DEFAULT
    info = spec(name)
    ready = installed(name) and dependencies_ok()
    gpu = ready and gpu_available()
    where = 'on this PC’s graphics card' if gpu else 'on this PC'
    detail = (f"{info['label']} · {info['languages']} · runs {where}" if ready else
              'Needs the tokenizers package (run Setup-Alfred.cmd again)' if installed(name) else
              f"Not downloaded ({info['size_mb']} MB)")
    return {'name': name, 'label': info['label'], 'size_mb': info['size_mb'], 'installed': installed(name), 'ready': ready,
            'enabled': bool(settings.get('rerank_enabled', True)), 'detail': detail, 'gpu': gpu,
            'gpu_model': gpu_model_installed(name), 'gpu_size_mb': info.get('gpu_size_mb')}


def for_settings(settings):
    """The loaded reranker to use, or None (turned off, not downloaded, or failed to load)."""
    if not settings.get('rerank_enabled', True):
        return None
    name = settings.get('rerank_model') or DEFAULT
    if not installed(name) or not dependencies_ok():
        return None
    with _load_lock:
        if name not in _loaded:
            try:
                _loaded[name] = Reranker(name)
            except Exception as error:  # a broken download never breaks answering
                log.warning('Reranker %s unavailable: %s', name, error)
                _loaded[name] = None
        return _loaded[name]


def forget():
    with _load_lock:
        _loaded.clear()


def download(name, progress=lambda done, total: None, cancel=None, gpu=False):
    """Fetch the model's files from Hugging Face (checked against their published SHA-256); with ``gpu``,
    the half-precision copy a graphics card runs fastest."""
    info = dict(spec(name))
    if gpu:
        info['files'] = info.get('gpu_files') or {}
    target = folder(name)
    target.mkdir(parents=True, exist_ok=True)
    listing = {}
    try:
        with urllib.request.urlopen(f"{HF}/api/models/{info['repo']}/tree/main/onnx", timeout=30) as response:
            listing.update({item['path']: item for item in json.load(response)})
        with urllib.request.urlopen(f"{HF}/api/models/{info['repo']}/tree/main", timeout=30) as response:
            listing.update({item['path']: item for item in json.load(response)})
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RerankError(f'Cannot reach huggingface.co: {getattr(error, "reason", error)}') from None
    total = sum(int((listing.get(remote) or {}).get('size') or 0) for remote in info['files'].values())
    done = 0
    for local, remote in info['files'].items():
        entry = listing.get(remote) or {}
        expected = (entry.get('lfs') or {}).get('oid')
        final = target / local
        if final.is_file() and final.stat().st_size == int(entry.get('size') or -1):
            done += final.stat().st_size
            continue
        part = final.with_suffix(final.suffix + '.part')
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(f"{HF}/{info['repo']}/resolve/main/{remote}", timeout=60) as response, open(part, 'wb') as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise RerankError('Download stopped')
                    block = response.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    progress(done, total)
        except (urllib.error.URLError, OSError) as error:
            part.unlink(missing_ok=True)
            raise RerankError(f'Download failed: {getattr(error, "reason", error)}') from None
        except RerankError:
            part.unlink(missing_ok=True)
            raise
        if expected and digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise RerankError(f'{local} did not match its published checksum; nothing was kept')
        part.replace(final)
    forget()
    return {'name': name, 'folder': str(target), 'bytes': done}


def remove(name):
    for local in list(spec(name)['files']) + list(spec(name).get('gpu_files', {})):
        (folder(name) / local).unlink(missing_ok=True)
    forget()


class Reranker:
    def __init__(self, name):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.name, self.info = name, spec(name)
        self.threshold = self.info['threshold']
        base = folder(name)
        self.tokenizer = Tokenizer.from_file(str(base / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=self.info['max_length'], strategy='only_second')
        self.tokenizer.no_padding()
        self.pad = self.tokenizer.token_to_id('<pad>') if self.tokenizer.token_to_id('<pad>') is not None else 0
        self.seconds = 0.0
        self.lock = threading.Lock()  # one run at a time (DirectML sessions aren't safe to share across threads)
        self.session = None
        if gpu_available():
            try:
                self._open(ort, 'gpu')
                self.score('warm up', ['Alfred loads the reranker.'])  # the first GPU run compiles its kernels
            except Exception as error:
                log.warning('Reranker on the GPU unavailable (%s); using the CPU', error)
                self.session = None
        if self.session is None:
            self._open(ort, 'cpu')

    def _open(self, ort, where):
        base = folder(self.name)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if where == 'gpu':
            options.enable_mem_pattern = False  # DirectML requirements
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            offered = ort.get_available_providers()
            providers = [p for p in ('DmlExecutionProvider', 'CUDAExecutionProvider') if p in offered] + ['CPUExecutionProvider']
            model = base / 'model_fp16.onnx' if gpu_model_installed(self.name) else base / 'model.onnx'
        else:
            options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4) // 2))
            providers, model = ['CPUExecutionProvider'], base / 'model.onnx'
        self.session = ort.InferenceSession(str(model), options, providers=providers)
        self.device = 'GPU' if self.session.get_providers()[0] != 'CPUExecutionProvider' else 'CPU'
        self.precision = 'fp16' if model.name == 'model_fp16.onnx' else 'int8'
        self.inputs = {i.name for i in self.session.get_inputs()}

    def score(self, query, passages, batch=8, cancel=None):
        """Relevance in [0, 1] of each passage to ``query`` (same order as ``passages``)."""
        if not passages:
            return []
        started = time.time()
        encodings = self.tokenizer.encode_batch([(query, passage) for passage in passages])
        order = sorted(range(len(encodings)), key=lambda n: len(encodings[n].ids))  # similar lengths batch with less padding
        scores = np.zeros(len(passages), dtype=np.float32)
        for start in range(0, len(order), batch):
            if cancel is not None and cancel.is_set():
                break
            chunk = order[start:start + batch]
            width = max(len(encodings[n].ids) for n in chunk)
            ids = np.full((len(chunk), width), self.pad, dtype=np.int64)
            mask = np.zeros((len(chunk), width), dtype=np.int64)
            types = np.zeros((len(chunk), width), dtype=np.int64)
            for row, n in enumerate(chunk):
                encoding = encodings[n]
                ids[row, :len(encoding.ids)] = encoding.ids
                mask[row, :len(encoding.ids)] = 1
                types[row, :len(encoding.type_ids)] = encoding.type_ids
            feed = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': types}
            logits = self._run({k: v for k, v in feed.items() if k in self.inputs})
            logits = np.asarray(logits, dtype=np.float32).reshape(len(chunk), -1)[:, 0]
            scores[chunk] = 1.0 / (1.0 + np.exp(-logits))
        self.seconds = time.time() - started
        return [float(s) for s in scores]

    def _run(self, feed):
        with self.lock:
            try:
                return self.session.run(None, feed)[0]
            except Exception as error:
                if self.device != 'GPU':
                    raise
                log.warning('Reranker failed on the GPU (%s); switching to the CPU', error)  # a driver reset, out of memory…
                import onnxruntime as ort
                self._open(ort, 'cpu')
                return self.session.run(None, feed)[0]
