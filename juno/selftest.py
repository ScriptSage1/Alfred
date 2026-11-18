"""``Juno.exe --selftest [folder]`` (or ``python -m juno --selftest``): check a build without opening a window.

Everything runs in a fresh temporary data folder (JUNO_HOME is pointed at it in this process and the folder is
removed at the end), with Qt's offscreen platform. Nothing is downloaded, no llama.cpp server is started, the running
Juno is not contacted and the real data folder is never touched. One line per check goes to standard output (when
there is one: a windowed Juno.exe has none) and to the log file (``--selftest-log PATH``, default
%TEMP%\\juno-selftest.log). The last line is ``SELFTEST OK`` (exit code 0) or ``SELFTEST FAILED: ...`` (exit code 1).
"""
import os
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

WORDS = {'text': 'zanzibar harbour pilot', 'docx': 'quokka budget approved', 'pdf': 'marmalade invoice summary',
         'ocr': ('JUNO', '2468')}


class Log:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'w', encoding='utf-8')
        self.failures = []

    def line(self, text):
        self.file.write(text + '\n')
        self.file.flush()
        if sys.stdout is not None:
            try:
                print(text, flush=True)
            except (OSError, ValueError):
                pass

    def check(self, name, fn):
        started = time.perf_counter()
        try:
            detail = fn()
        except Exception as error:  # every check runs; the summary names the ones that failed
            self.failures.append(name)
            self.line(f'FAIL {name}: {type(error).__name__}: {error}')
            for row in traceback.format_exc().rstrip().splitlines()[-6:]:
                self.line(f'     {row}')
            return None
        self.line(f'ok   {name} ({time.perf_counter() - started:.1f}s){": " + str(detail) if detail else ""}')
        return detail

    def close(self):
        self.file.close()


def _arguments(argv):
    """(work folder or None, log path) from the arguments after --selftest."""
    rest = list(argv[argv.index('--selftest') + 1:]) if '--selftest' in argv else []
    log = None
    if '--selftest-log' in argv:
        at = argv.index('--selftest-log')
        log = argv[at + 1] if at + 1 < len(argv) else None
        rest = [a for i, a in enumerate(rest) if a != '--selftest-log' and (i == 0 or rest[i - 1] != '--selftest-log')]
    folder = next((a for a in rest if not a.startswith('--')), None)
    return folder, log or os.path.join(tempfile.gettempdir(), 'juno-selftest.log')


# ------------------------------------------------------------------ sample files
def _docx(path, paragraphs):
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ''.join(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document {namespace}><w:body>{body}</w:body></w:document>')


def _pdf(path):
    """One page of text followed by a ruled table (the worker-process table reader looks at it)."""
    import pymupdf
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), f'The {WORDS["pdf"]} for the self-test.', fontsize=12)
    left, top, width, height = 72, 120, 150, 24
    rows = [('Item', 'Count', 'Price'), ('Apples', '3', '1.20'), ('Pears', '5', '2.40'), ('Plums', '7', '3.60')]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((left + c * width + 6, top + r * height + 16), cell, fontsize=11)
    for r in range(len(rows) + 1):
        page.draw_line((left, top + r * height), (left + 3 * width, top + r * height))
    for c in range(4):
        page.draw_line((left + c * width, top), (left + c * width, top + len(rows) * height))
    document.save(str(path))
    document.close()


def _png(path):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (900, 220), 'white')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('arial.ttf', 96)
    except OSError:
        font = ImageFont.load_default(96)
    draw.text((40, 50), ' '.join(WORDS['ocr']), fill='black', font=font)
    image.save(path)


# ------------------------------------------------------------------ checks
def _qt():
    from PySide6 import QtCore, QtGui, QtNetwork, QtSvg, QtWidgets  # noqa: F401  (every Qt module Juno imports)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['juno-selftest'])
    formats = {bytes(f).decode() for f in QtGui.QImageReader.supportedImageFormats()}
    missing = {'svg', 'ico', 'png', 'jpg'} - formats
    if missing:
        raise RuntimeError(f'Qt image formats missing: {sorted(missing)}')
    widget = QtWidgets.QLabel('Juno')
    widget.resize(120, 40)
    widget.grab()  # paints with the offscreen platform
    return f'Qt {QtCore.qVersion()}, platform {app.platformName()}'


def _numpy_pillow():
    import numpy
    import PIL
    return f'numpy {numpy.__version__}, Pillow {PIL.__version__}'


def _pymupdf():
    import pymupdf
    return f'PyMuPDF {pymupdf.VersionBind}'


def _onnxruntime():
    import onnxruntime
    return f'onnxruntime {onnxruntime.__version__}, providers {",".join(onnxruntime.get_available_providers())}'


def _rapidocr():
    import rapidocr
    folder = Path(rapidocr.__file__).parent
    models = sorted(p.name for p in (folder / 'models').glob('*.onnx'))
    if len(models) < 3 or not (folder / 'config.yaml').is_file():
        raise RuntimeError(f'RapidOCR model files missing in {folder} (found {models})')
    return f'{len(models)} models'


def _ffmpeg():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not os.path.isfile(exe):
        raise RuntimeError(f'ffmpeg not found at {exe}')
    return os.path.basename(exe)


def _keyring():
    import keyring
    backend = keyring.get_keyring()  # only the backend is looked up; no credential is read or written
    name = type(backend).__module__ + '.' + type(backend).__name__
    if 'fail' in name.lower() or 'null' in name.lower():
        raise RuntimeError(f'no usable keyring backend ({name})')
    return name


def _tokenizers():
    import tokenizers
    return f'tokenizers {tokenizers.__version__}'


def _sqlite():
    import sqlite3
    con = sqlite3.connect(':memory:')
    try:
        con.execute('CREATE VIRTUAL TABLE t USING fts5(body)')
        con.execute("INSERT INTO t VALUES ('juno finds files')")
        if not con.execute("SELECT count(*) FROM t WHERE t MATCH 'finds'").fetchone()[0]:
            raise RuntimeError('FTS5 query found nothing')
    finally:
        con.close()
    return f'SQLite {sqlite3.sqlite_version} with FTS5'


def _app_modules():
    import importlib
    names = ('ui', 'ui_kit', 'mcp_server', 'rag', 'providers', 'chats', 'glass', 'hotkey', 'watcher', 'picker', 'rerank',
             'runtime', 'models', 'ai', 'keys', 'opener', 'proof', 'power', 'render', 'startup')
    for name in names:
        importlib.import_module(f'juno.{name}')
    return f'{len(names)} modules'


def _data_files():
    from PySide6.QtGui import QIcon
    from PySide6.QtSvg import QSvgRenderer
    from . import runtime, startup, ui_kit
    assets = startup.ASSETS
    needed = [assets / 'juno.ico', assets / 'juno.png', Path(ui_kit.APP_LOGO), runtime.REFERENCE]
    needed += [Path(ui_kit.LOGO_DIR) / f'{name}.svg' for name in ('anthropic', 'claude', 'openai', 'pc')]
    absent = [str(p) for p in needed if not p.is_file()]
    if absent:
        raise RuntimeError(f'missing: {absent}')
    if not QSvgRenderer(str(ui_kit.APP_LOGO)).isValid():
        raise RuntimeError('juno.svg does not render')
    if QIcon(str(assets / 'juno.ico')).isNull():
        raise RuntimeError('juno.ico does not load')
    return f'{len(needed)} files'


def _engine(home):
    """Where search by meaning's engine and model are (looks only; nothing is started). A build ships both, so in a
    frozen build a missing one is a packaging fault."""
    from . import models, runtime
    engine = runtime.program_dir(home)
    model = models.find('embed-text', home)
    if getattr(sys, 'frozen', False):
        absent = [name for name, found in (('llama.cpp engine', engine), ('search model', model)) if found is None]
        if absent:
            raise RuntimeError(f'{" and ".join(absent)} missing from this build (packaging\\bundle_models.py)')
    return (f'llama.cpp engine at {engine}' if engine else 'llama.cpp engine not installed') + ', ' + \
        (f'search model at {model}' if model else 'search model not on this PC (keyword search only)')


def _index(home, folder):
    from . import config
    from .index import Index
    settings = config.load(home)
    settings.update({'tracked_folders': [str(folder)], 'embed_enabled': False, 'ocr_device': 'cpu', 'picture_search': False})
    index = Index(settings)
    try:
        index.update()
        found = {}
        for kind in ('text', 'docx', 'pdf'):
            query = WORDS[kind].split()[0]
            results = index.search(query, limit=5)
            found[kind] = [r['filename'] for r in results]
        expected = {'text': 'notes.txt', 'docx': 'report.docx', 'pdf': 'invoice.pdf'}
        wrong = {k: v for k, v in found.items() if expected[k] not in v}
        if wrong:
            raise RuntimeError(f'keyword search did not find {wrong}')
        status = index.status()
        return f"{status.get('files')} files, {status.get('segments')} passages, found {', '.join(expected.values())}"
    finally:
        index.close()


def _ocr(png):
    from . import ocr
    ocr.configure('cpu', 2)
    text = ocr.read_file(png).upper()
    if not all(word in text for word in WORDS['ocr']):
        raise RuntimeError(f'OCR read {text!r}')
    return repr(text)


def _pdf_render(pdf):
    from . import extract
    [(number, image)] = extract.pdf_page_images(pdf, [1], side=256)
    if max(image.size) != 256:
        raise RuntimeError(f'rendered page is {image.size}')
    return f'page {number} rendered {image.size[0]}x{image.size[1]}'


def _worker(pdf):
    from concurrent.futures import ProcessPoolExecutor

    from . import extract
    with ProcessPoolExecutor(1) as pool:
        read = pool.submit(extract.read_table_pages, str(pdf), [1]).result(timeout=180)
    if not isinstance(read, dict):
        raise RuntimeError(f'worker returned {read!r}')
    rows = sum(len(p) for p in read.values())
    return f'worker process ran ({rows} table passage(s))'


def run(argv):
    """Run every check; returns the exit code."""
    folder, log_path = _arguments(argv)
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    fonts = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    if os.path.isdir(fonts):
        os.environ.setdefault('QT_QPA_FONTDIR', fonts)
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='juno-selftest-', dir=folder))
    home, files = work / 'home', work / 'files'
    home.mkdir()
    files.mkdir()
    previous = os.environ.get('JUNO_HOME')
    os.environ['JUNO_HOME'] = str(home)  # anything that asks for the data folder gets the temporary one
    log = Log(log_path)
    try:
        from . import __version__
        log.line(f'Juno {__version__} self-test, {"frozen build" if getattr(sys, "frozen", False) else "Python " + sys.version.split()[0]}'
                 f', in {work}')
        log.check('Qt widgets (offscreen)', _qt)
        log.check('numpy and Pillow', _numpy_pillow)
        log.check('PyMuPDF', _pymupdf)
        log.check('onnxruntime', _onnxruntime)
        log.check('RapidOCR and its models', _rapidocr)
        log.check('ffmpeg (imageio-ffmpeg)', _ffmpeg)
        log.check('keyring backend', _keyring)
        log.check('tokenizers', _tokenizers)
        log.check('sqlite3 FTS5', _sqlite)
        log.check('Juno modules import', _app_modules)
        log.check('app data files', _data_files)
        log.check('search by meaning files', lambda: _engine(home))
        made = log.check('sample files', lambda: (
            (files / 'notes.txt').write_text(f'Meeting notes: the {WORDS["text"]} arrives at noon.\n', encoding='utf-8'),
            _docx(files / 'report.docx', [WORDS['docx'], 'Second paragraph of the report.']),
            _pdf(files / 'invoice.pdf'), _png(files / 'picture.png'), '4 files')[-1])
        if made:
            log.check('index and keyword search (no embedder)', lambda: _index(home, files))
            log.check('OCR of a picture', lambda: _ocr(files / 'picture.png'))
            log.check('PDF page render', lambda: _pdf_render(files / 'invoice.pdf'))
            log.check('PDF table worker process', lambda: _worker(files / 'invoice.pdf'))
        if log.failures:
            log.line(f'SELFTEST FAILED: {", ".join(log.failures)}')
            return 1
        log.line('SELFTEST OK')
        return 0
    except BaseException as error:
        log.line(f'SELFTEST FAILED: {type(error).__name__}: {error}')
        return 1
    finally:
        log.close()
        if previous is None:
            os.environ.pop('JUNO_HOME', None)
        else:
            os.environ['JUNO_HOME'] = previous
        shutil.rmtree(work, ignore_errors=True)
