"""Local OCR: RapidOCR (PP-OCR models on onnxruntime, CPU). The models ship with the
package, so reading text never needs the network. The engine loads on first use."""
import logging
import threading

import numpy as np
from PIL import Image, ImageOps

MIN_SCORE = 0.5
MAX_SIDE = 2400  # larger images are scaled down; OCR accuracy doesn't improve past this
_engine = None
_lock = threading.Lock()


def engine():
    global _engine
    with _lock:
        if _engine is None:
            for name in ('RapidOCR', 'rapidocr'):
                logging.getLogger(name).setLevel(logging.WARNING)
            from rapidocr import RapidOCR
            _engine = RapidOCR()
            for name in ('RapidOCR', 'rapidocr'):
                logging.getLogger(name).setLevel(logging.WARNING)
        return _engine


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
    result = engine()(prepare(image))
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
