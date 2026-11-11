"""Local OCR: RapidOCR (PP-OCR models on ONNX Runtime). The models ship with the package, so reading text never
needs the network.

Engines live in a small pool that ``read_text`` borrows from, planned by ``configure``:
* DirectML (the GPU, through ``onnxruntime-directml``): one engine. It must read a generated test picture
  correctly when it is created; otherwise OCR falls back to the CPU for the rest of the process.
* CPU: up to four engines, each limited to a few threads, so ONNX Runtime doesn't oversubscribe the machine
  (left alone it picks a thread count for every core, and is three times slower on a 20-thread laptop).
"""
import logging
import queue
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger('alfred')
MIN_SCORE = 0.5
MAX_SIDE = 2400  # larger images are scaled down; OCR accuracy doesn't improve past this
_lock = threading.Lock()
_plan = None          # (device, engines, threads per engine) in use
_pool = None          # queue of idle engines for _plan
_created = 0          # engines created for _plan
_generation = 0       # bumped when the plan changes: engines of an older plan are dropped when returned
_dml_failed = False   # DirectML failed its self-check (or to load) once: the CPU for the rest of the process


def _quiet():
    for name in ('RapidOCR', 'rapidocr'):
        logging.getLogger(name).setLevel(logging.WARNING)


def thread_plan(budget):
    """(engines, threads per engine) for a CPU thread budget."""
    budget = max(1, int(budget or 1))
    engines = max(1, min(4, budget // 4))
    return engines, max(1, min(4, budget // engines))


def wants_gpu(device):
    """Whether ``device`` ('auto' | 'gpu' | 'cpu') asks for DirectML on this PC."""
    if device == 'cpu':
        return False
    if device == 'gpu':
        return True
    from . import ai
    return ai.has_discrete_gpu()


def configure(device='auto', budget=4):
    """Plan the engine pool for ``device`` and a CPU thread ``budget``; engines are created on first use.
    Returns the device that will be used: 'directml' or 'cpu'."""
    global _plan, _pool, _created, _generation
    use_dml = wants_gpu(device) and not _dml_failed
    plan = ('directml', 1, 1) if use_dml else ('cpu', *thread_plan(budget))
    with _lock:
        if plan != _plan:
            _plan, _pool, _created = plan, queue.Queue(), 0
            _generation += 1
        return _plan[0]


def device():
    """'directml' or 'cpu': what OCR runs on now (configure first)."""
    with _lock:
        return (_plan or ('cpu',))[0]


def engine_count():
    """How many engines the pool holds (how many pictures can be read at once)."""
    with _lock:
        return (_plan or ('cpu', 1, 1))[1]


def _make(kind, threads):
    _quiet()
    from rapidocr import RapidOCR
    if kind == 'directml':
        engine = RapidOCR(params={'EngineConfig.onnxruntime.use_dml': True})
    else:
        engine = RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads': int(threads)})
    _quiet()
    return engine


def self_check(engine):
    """Whether ``engine`` reads a generated picture of large black text correctly."""
    image = Image.new('RGB', (900, 220), 'white')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('arial.ttf', 96)
    except OSError:
        font = ImageFont.load_default(96)
    draw.text((40, 50), 'ALFRED 2468', fill='black', font=font)
    result = engine(np.asarray(image))
    text = ' '.join(getattr(result, 'txts', None) or ()).upper()
    return 'ALFRED' in text and '2468' in text


def _borrow():
    """(engine, generation) from the pool, creating one when the pool may still grow."""
    global _plan, _pool, _created, _generation, _dml_failed
    if _plan is None:
        configure('cpu', 4)
    while True:
        with _lock:
            plan, pool, generation = _plan, _pool, _generation
            try:
                return pool.get_nowait(), generation
            except queue.Empty:
                create = _created < plan[1]
                if create:
                    _created += 1
        if not create:
            try:
                return pool.get(timeout=1.0), generation
            except queue.Empty:
                continue  # the plan may have changed meanwhile: look again
        try:
            engine = _make(plan[0], plan[2])
            if plan[0] == 'directml' and not self_check(engine):
                raise RuntimeError('the GPU misread the test picture')
            return engine, generation
        except Exception as error:
            if plan[0] != 'directml':
                with _lock:
                    if generation == _generation:
                        _created -= 1
                raise
            from . import power
            with _lock:
                if not _dml_failed:
                    log.warning('OCR on the GPU (DirectML) is unavailable, using the CPU: %s', error)
                _dml_failed = True
                if generation == _generation:
                    _plan, _pool, _created = ('cpu', *thread_plan(max(1, power.usable_cpus() - 2))), queue.Queue(), 0
                    _generation += 1


def _return(engine, generation):
    with _lock:
        if generation == _generation:
            _pool.put(engine)


def engine():
    """An engine for one-off use (kept for callers outside the pool)."""
    item, generation = _borrow()
    _return(item, generation)
    return item


def prepare(image):
    """RGB numpy array, EXIF-rotated and bounded in size."""
    image = ImageOps.exif_transpose(image)
    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGBA')
        background = Image.new('RGB', image.size, 'white')
        background.paste(image, mask=image.getchannel('A'))
        image = background
    image = image.convert('RGB')
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    return np.asarray(image)


def read_text(image):
    """Text in a PIL image, as lines in reading order (top to bottom, left to right)."""
    if min(image.size) < 8:
        return ''
    array = prepare(image)
    item, generation = _borrow()
    try:
        result = item(array)
    finally:
        _return(item, generation)
    if result is None or not getattr(result, 'txts', None):
        return ''
    lines = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        if score < MIN_SCORE or not text.strip():
            continue
        box = np.asarray(box)
        lines.append((float(box[:, 1].min()), float(box[:, 0].min()), float(box[:, 1].max() - box[:, 1].min()), text.strip()))
    lines.sort()
    rows, current, last_top, last_height = [], [], None, 0
    for top, left, height, text in lines:
        if last_top is not None and top - last_top > max(last_height, height) * 0.6:
            rows.append(current)
            current = []
        current.append((left, text))
        last_top, last_height = top, height
    if current:
        rows.append(current)
    return '\n'.join(' '.join(text for _, text in sorted(row)) for row in rows)


def read_file(path):
    with Image.open(path) as image:
        image.seek(0)  # first frame of animated GIF/TIFF
        return read_text(image.copy())
