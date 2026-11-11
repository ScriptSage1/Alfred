"""The search index: every tracked folder in one SQLite database (WAL).

* Incremental: a file is re-read only when its size or modification time changes;
  deleted files and untracked folders leave the index.
* Keyword search: SQLite FTS5 with BM25 (file names weigh more than body text).
* Semantic search: EmbeddingGemma 2 vectors (normalised float32), compared with a
  dot product; vectors are cached per passage and rebuilt when the model changes.
* Hybrid: reciprocal-rank fusion of both lists. Keyword search keeps working
  when Ollama is unavailable.
* Enriched passages: both searches see each passage with a short header in front
  (file, folder, slide title / sheet columns / section heading, place), and,
  when turned on, a sentence or two of AI-written context situating it in its
  document (Anthropic's "contextual retrieval"); the stored text stays as read.
* In layers: an update makes every file findable by name first, then keyword-searchable (documents' text),
  then searchable by meaning; OCR reads pictures, videos and scanned PDF pages on worker threads meanwhile,
  and pictures get a vector of what they show (the embedding model reads pictures). Each layer is committed
  as it completes, so a stopped run loses nothing. Only the update thread writes to the database.
* Pictures by what they show: a text query is compared with picture vectors too ('picture' results, with
  their own confidence floor); AI captions are optional enrichment, written in the background or on demand.
"""
import collections
import functools
import hashlib
import itertools
import json
import logging
import math
import os
import queue
import re
import sqlite3
import stat as file_stat
import threading
import time
import unicodedata
import weakref
from pathlib import Path

import numpy as np

from . import ai, extract, ocr, paths, power
from .extract import IMAGE_SUFFIXES, KIND_SUFFIXES, VIDEO_SUFFIXES, location_label, video_frame_png  # noqa: F401 (re-exported for the UI)

log = logging.getLogger('alfred')
RRF_K = 60
PHRASE_BOOST = 1.0  # per identifier a passage contains exactly; fused RRF scores stay below 2 / RRF_K
IDENTIFIER_SEMANTIC_WEIGHT = 0.25  # meaning search knows little about codes and numbers: it votes less for them
DESCRIBE_EMBED_EVERY = 25  # embed new AI descriptions in batches while describing a large folder
SEMANTIC_BAND = 0.06  # meaning matches must be this close to the best one (EmbeddingGemma cosine)
ENRICH_VERSION = 'header-v1'  # bump when chunk headers change: search text is rebuilt and passages re-embedded
PDF_READER = 'tables-v1'  # bump when PDF tables are read differently: every PDF's tables are read again
REREAD_FOR_ENRICH = {'.docx', '.pptx', '.xlsx', '.md', '.markdown', '.txt'}  # their headings/titles are read since header-v1
CONTEXT_KINDS = ('text', 'page', 'slide', 'sheet', 'ocr', 'video')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, root TEXT, mtime_ns INTEGER, size INTEGER, status TEXT, error TEXT,
                                 passages INTEGER, indexed_at REAL);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, kind TEXT, text TEXT, location TEXT);
CREATE INDEX IF NOT EXISTS segments_path ON segments(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, name, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vectors(segment_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS descriptions(path TEXT PRIMARY KEY, signature TEXT, status TEXT, error TEXT, seconds REAL, described_at REAL);
CREATE TABLE IF NOT EXISTS contexts(key TEXT PRIMARY KEY, signature TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS pdf_tables(path TEXT PRIMARY KEY, reader TEXT);
CREATE TABLE IF NOT EXISTS layers(path TEXT, layer TEXT, signature TEXT, status TEXT, error TEXT, detail TEXT,
                                  seconds REAL, done_at REAL, PRIMARY KEY(path, layer));
CREATE TABLE IF NOT EXISTS media_cache(hash TEXT, layer TEXT, signature TEXT, payload BLOB, used_at REAL,
                                       PRIMARY KEY(hash, layer, signature));
'''
# files.status: 'pending' (found, text not read yet) | 'ok' | 'error' | 'unsupported'.
# segments.kind: 'file' (name only), 'text', 'page', 'slide', 'sheet', 'ocr', 'video', 'vision' (AI description, ord -1)
# and 'picture' (a picture vector: no text and no fts row, ord -2, -3, …).
# layers: per file, the 'ocr' and 'picture' layers (status 'pending' | 'ok' | 'error'; detail {"pages": [...]} for the
# pages of a PDF that need OCR / a picture vector). media_cache: OCR passages and picture vectors by content hash.
SCHEMA_VERSION = '2'
OCR_VERSION = 'rapidocr-v1'
PICTURE_VERSION = 'picture-v1:512'
CACHE_DAYS = 180
MEDIA = IMAGE_SUFFIXES | VIDEO_SUFFIXES
MEDIA_SQL = '(' + ' OR '.join(f"lower(f.path) LIKE '%{suffix}'" for suffix in sorted(MEDIA)) + ')'
_LOSSLESS = {'.png', '.bmp', '.gif', '.webp', '.tif', '.tiff'}
_Stat = collections.namedtuple('_Stat', 'st_mtime_ns st_size')


class IndexBusy(RuntimeError):
    pass


def connect(db_path):
    con = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    _upgrade(con)
    return con


def _upgrade(con):
    """Bring an index built by an older Alfred to SCHEMA_VERSION. Pictures, videos and PDFs it read with OCR are
    marked as OCR'd (nothing is read again); their picture vectors are simply missing, and added by layer 5."""
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row and row[0] == SCHEMA_VERSION:
        return
    with con:
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('schema_version') == SCHEMA_VERSION:
            return
        media = meta.get('media_signature') or ''
        if media.startswith('ocr:True'):
            interval = media.rsplit('video:', 1)[-1] if 'video:' in media else '10'
            now = time.time()
            for (path,) in list(con.execute("SELECT path FROM files WHERE status='ok'")):
                suffix = Path(path).suffix.lower()
                if suffix in MEDIA or suffix == '.pdf':
                    con.execute('INSERT OR IGNORE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                                (path, 'ocr', ocr_signature(path, interval), 'ok', '', None, 0, now))
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('ocr_enabled', '1'))
        elif media:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('ocr_enabled', '0'))
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('schema_version', SCHEMA_VERSION))


def ocr_signature(path, interval=10):
    """What a file's OCR was read with (a video also with its frame interval)."""
    return f'{OCR_VERSION}|video:{interval}' if Path(path).suffix.lower() in VIDEO_SUFFIXES else OCR_VERSION


def _ocr_cache_signature(path, interval=10):
    """Key of OCR results in media_cache: a PDF's OCR read holds its tables too."""
    return f'{OCR_VERSION}|{PDF_READER}' if Path(path).suffix.lower() == '.pdf' else ocr_signature(path, interval)


def picture_signature(embedder):
    return f'{embedder.signature()}|{PICTURE_VERSION}'


def _name_only(path):
    return ('file', Path(path).stem, {'name_only': True})


def _text_order(path):
    """Layer 2 order: pictures and videos (only reset), text-like files, Office files, PDFs, the rest."""
    suffix = Path(path).suffix.lower()
    return (0 if suffix in MEDIA else 1 if suffix in extract.TEXT_SUFFIXES else 2 if suffix in extract.OFFICE_SUFFIXES
            else 3 if suffix == '.pdf' else 4)


def _ocr_priority(path):
    """OCR queue order: scanned PDFs, lossless pictures (screenshots, scans), photos, then videos."""
    suffix = Path(path).suffix.lower()
    return 0 if suffix == '.pdf' else 1 if suffix in _LOSSLESS else 2 if suffix in IMAGE_SUFFIXES else 3


def file_hash(path, big=64 * 2 ** 20, edge=2 ** 20):
    """SHA-1 of a file's bytes (over ``big``: its size and its first and last ``edge`` bytes)."""
    digest = hashlib.sha1()
    size = os.path.getsize(path)
    with open(path, 'rb') as handle:
        if size > big:
            digest.update(str(size).encode())
            digest.update(handle.read(edge))
            handle.seek(-edge, os.SEEK_END)
            digest.update(handle.read(edge))
        else:
            for chunk in iter(lambda: handle.read(2 ** 20), b''):
                digest.update(chunk)
    return digest.hexdigest()


def _pack_pictures(found):
    """media_cache payload for [(location, vector)]: a JSON line, then the float32 vectors."""
    vectors = [np.asarray(vector, dtype=np.float32) for _, vector in found]
    dim = int(vectors[0].size) if vectors else 0
    head = json.dumps({'locations': [loc for loc, _ in found], 'dim': dim}).encode('utf-8')
    return head + b'\n' + b''.join(v.tobytes() for v in vectors)


def _unpack_pictures(blob):
    head, _, body = bytes(blob).partition(b'\n')
    info = json.loads(head)
    if not info['dim']:
        return []
    vectors = np.frombuffer(body, dtype=np.float32).reshape(-1, info['dim'])
    return list(zip(info['locations'], vectors))


def _picture_frames(path, pages=None):
    """[(location, PIL image)] to embed for one file: the picture, up to 8 keyframes of a video, or the given
    pages of a PDF, each at most ai.PICTURE_SIDE pixels on its longest side."""
    suffix = Path(path).suffix.lower()
    side = ai.PICTURE_SIDE
    if suffix in VIDEO_SUFFIXES:
        return [({'visual': True, 'seconds': seconds}, image) for seconds, image in extract.video_keyframes(path, 8, side)]
    if suffix == '.pdf':
        return [({'visual': True, 'page': number}, image) for number, image in extract.pdf_page_images(path, pages or [], side)]
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        image.seek(0)
        picture = ImageOps.exif_transpose(image).convert('RGB')
    picture.thumbnail((side, side))
    return [({'visual': True}, picture)]


class _OcrRunner:
    """OCR worker threads (layer 4). They read files and return passages; only the update thread writes them
    (it drains ``ready`` between its own work). A file whose content is already in media_cache is not read."""

    def __init__(self, workers, interval, cancel, cached):
        self.workers, self.interval, self.cancel, self.cached = max(1, int(workers)), interval, cancel, cached
        self.jobs, self.results = queue.PriorityQueue(), queue.Queue()
        self.order = itertools.count()
        self.queued, self.threads = set(), []
        self.total = self.outstanding = 0

    def submit(self, path, again=False):
        if path in self.queued and not again:
            return
        if not again:
            self.queued.add(path)
            self.total += 1
        self.outstanding += 1
        if not self.threads:
            self.threads = [threading.Thread(target=self._work, name='alfred-ocr', daemon=True) for _ in range(self.workers)]
            for thread in self.threads:
                thread.start()
        self.jobs.put((_ocr_priority(path), next(self.order), path))

    def _work(self):
        while True:
            _, _, path = self.jobs.get()
            if path is None:
                return
            self.results.put(self._read(path))

    def _read(self, path):
        result = {'path': path}
        if self.cancel.is_set():
            result['cancelled'] = True
            return result
        try:
            before = os.stat(path)
            result['stat'] = (before.st_mtime_ns, before.st_size)
            result['hash'] = file_hash(path)
            if (result['hash'], _ocr_cache_signature(path, self.interval)) in self.cached:
                result['cached'] = True
                return result
            began = time.time()
            result['passages'] = [list(p) for p in extract.extract(path, True, self.interval, None, self.cancel, pdf_tables=True)]
            result['seconds'] = time.time() - began
            if self.cancel.is_set():
                result['cancelled'] = True  # a video read only in part
            after = os.stat(path)
            if (after.st_mtime_ns, after.st_size) != result['stat']:
                result['changed'] = True
        except extract.Unsupported as reason:
            result['error'] = str(reason)
        except Exception as error:  # a bad file never stops the run
            result['error'] = (str(error) or type(error).__name__)[:300]
            log.warning('Could not read the text in %s: %s', path, result['error'])
        return result

    def ready(self, wait=None):
        """Finished results (waiting up to ``wait`` seconds for the first one)."""
        found = []
        try:
            if wait:
                found.append(self.results.get(timeout=wait))
            while True:
                found.append(self.results.get_nowait())
        except queue.Empty:
            pass
        self.outstanding -= len(found)
        return found

    def close(self):
        """Stop the workers: queued jobs are dropped, a job being read finishes (and is not stored)."""
        try:
            while True:
                self.jobs.get_nowait()
        except queue.Empty:
            pass
        for _ in self.threads:
            self.jobs.put((99, next(self.order), None))
        if not self.cancel.is_set():
            for thread in self.threads:
                thread.join(timeout=10)


class _Run:
    """One update's progress reporting: every state carries the four layers as [done, total]."""

    def __init__(self, index, con, progress, summary):
        self.index, self.con, self.progress, self.summary = index, con, progress, summary
        self.layers = {'names': [0, 0], 'text': [0, 0], 'meaning': [0, 0], 'pictures': [0, 0]}
        self.meaning_base = 0
        self.ocr = None
        self.hashes = {}  # path -> ((mtime, size), content hash), from OCR, reused by the picture layer

    def refresh(self):
        try:
            self.layers = self.index._layer_counts(self.con)
        except sqlite3.Error:
            pass
        self.meaning_base = self.layers['meaning'][0]

    def emit(self, state):
        if state.get('phase') == 'embedding':
            self.layers['meaning'][0] = min(self.layers['meaning'][1], self.meaning_base + state.get('done', 0))
        self.progress(dict(state, layers={key: list(value) for key, value in self.layers.items()}))

    def quiet(self, phase):
        """A progress callback that reports under ``phase`` (embedding OCR text while reading pictures, ...)."""
        return lambda state: self.emit(dict(state, phase=phase))

    def apply_ocr(self, wait=None):
        if self.ocr is None:
            return
        for result in self.ocr.ready(wait):
            self.index._apply_ocr(self.con, self, result)
            pictures = self.layers['pictures']
            pictures[0] = min(pictures[1], pictures[0] + 1)

    def file_hash(self, path, stat):
        known = self.hashes.get(path)
        if known and known[0] == stat:
            return known[1]
        digest = file_hash(path)
        self.hashes[path] = (stat, digest)
        return digest


_MISSING = object()
_READER_SERIAL = itertools.count(1)


class _Reader:
    """A thread's read connection to the index. Closed when its thread ends (the thread-local holding it is
    dropped) or when the Index is closed."""
    __slots__ = ('con', 'serial', '__weakref__')

    def __init__(self, db_path):
        self.con = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.con.execute('PRAGMA query_only=1')
        self.serial = next(_READER_SERIAL)

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass

    __del__ = close


def name_terms(path):
    """File name words for the FTS name column: 'Q3_budget-final.xlsx' → 'Q3 budget final xlsx'."""
    return ' '.join(re.split(r'[\W_]+', Path(path).name))


def context_key(path, text):
    """Cache key of a passage's AI context: the same passage of the same file keeps it across re-reads."""
    return hashlib.sha1(f'{path}\n{text}'.encode('utf-8', 'replace')).hexdigest()


def enriched(header, context, text):
    """What keyword and meaning search see for a passage."""
    return '\n'.join(part for part in (header, context, text) if part)


def discover(root):
    """Yield (path, stat) for every regular file under ``root`` that Alfred may read.

    Symlinks, junctions, hidden/system entries, dot-folders and credential-looking
    files are skipped, and nothing outside ``root`` is ever returned.
    """
    root = Path(os.path.realpath(root))
    pending = [root]
    while pending:
        base = pending.pop()
        try:
            entries = list(os.scandir(base))
        except OSError as error:
            log.warning('Cannot read folder %s: %s', base, error)
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if file_stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & (paths.REPARSE | paths.HIDDEN_SYSTEM):
                continue
            if entry.is_dir(follow_symlinks=False):
                if not paths.skip_dir(entry.name):
                    pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False) and not entry.name.startswith(('.', '~$')) and not paths.looks_secret(entry.name):
                yield entry.path, st


class Index:
    def __init__(self, settings):
        self.settings = settings
        self.db_path = settings.db_path
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._vectors = (None, None, None, None)  # (version, ids, matrix, which rows are picture vectors)
        self._pause_check = None  # while a background update runs: True when it should pause (on battery)
        self._interval = 10
        self._describe_lock = threading.Lock()
        self._describing = {}  # path -> Event, while describe_now describes it
        self._query_cache = {}  # query vectors, and recent search results (see search)
        self._roots_cache = None  # (tracked folders as set, canonical roots, {path: (root, display folder)})
        self._local = threading.local()  # one read connection per thread (searches run on UI worker threads)
        self._readers = weakref.WeakSet()
        connect(self.db_path).close()  # the schema is created here, once; read connections skip it

    def _reader(self):
        """This thread's read connection, opened once (autocommit: every query sees the latest commit)."""
        reader = getattr(self._local, 'reader', None)
        if reader is None:
            reader = self._local.reader = _Reader(self.db_path)
            self._readers.add(reader)
        return reader.con

    def _index_version(self, con):
        """(this connection, its data_version): changes whenever any other connection commits to the index."""
        return self._local.reader.serial, con.execute('PRAGMA data_version').fetchone()[0]

    def close(self):
        """Close the read connections (the database file can then be deleted)."""
        for reader in list(self._readers):
            reader.close()
        self._readers = weakref.WeakSet()
        self._local = threading.local()

    # ---------------------------------------------------------------- folders
    def _tracked(self):
        """(key, canonical roots, place memo), rebuilt only when the tracked folders change."""
        key = tuple(str(folder) for folder in self.settings.get('tracked_folders') or [])
        cached = self._roots_cache
        if cached is None or cached[0] != key:
            cached = (key, [Path(os.path.realpath(folder)) for folder in self.settings.folders], {})
            self._roots_cache = cached
        return cached

    def roots(self):
        return list(self._tracked()[1])

    def root_of(self, path):
        return next((root for root in self.roots() if paths.is_within(root, path)), None)

    def _place(self, path):
        """(root, display folder) of an indexed file, or None when no tracked folder holds it. Indexed paths are
        already real paths (see discover), so this is a string comparison, remembered until the folders change."""
        _, roots, memo = self._tracked()
        place = memo.get(path, _MISSING)
        if place is not _MISSING:
            return place
        lowered = os.path.normcase(str(path))
        root = next((r for r in roots if lowered == os.path.normcase(str(r)) or
                     lowered.startswith(os.path.normcase(str(r)).rstrip(os.sep) + os.sep)), None)
        if root is None:  # not in real-path form: resolve it the slow way
            root = next((r for r in roots if paths.is_within(r, path)), None)
        place = None
        if root is not None:
            parent = Path(path).parent
            rel = parent.parts[len(root.parts):] if len(parent.parts) > len(root.parts) else ()
            place = (root, ' › '.join([root.name] + list(rel)))
        if len(memo) > 50000:
            memo.clear()
        memo[path] = place
        return place

    def resolve(self, path):
        """The real file behind a search result, only if it is still inside a tracked folder."""
        target = Path(os.path.realpath(path))
        if self.root_of(target) is None:
            raise paths.PathError('This file is no longer in a tracked folder')
        if not target.is_file():
            raise paths.PathError('File not found — it may have been moved or deleted')
        return target

    def display_folder(self, path):
        """'Downloads › Reports' for a file in Downloads\\Reports."""
        place = self._place(str(path))
        return place[1] if place else str(Path(path).parent)

    # ------------------------------------------------------------ enrichment
    def header(self, path, kind, loc):
        return extract.chunk_header(path, self.display_folder(path), kind, loc)

    def context_signature(self):
        """Signature of the AI chunk contexts in use, or None when that setting is off."""
        writer = ai.Contextualizer.from_settings(self.settings)
        return writer.signature() if writer else None

    def search_signature(self):
        return f'{ENRICH_VERSION}|context:{self.context_signature() or "off"}'

    @staticmethod
    def vector_signature(embedder):
        return f'{embedder.signature()}|{ENRICH_VERSION}'

    def _context(self, con, path, text, signature):
        if not signature:
            return ''
        row = con.execute('SELECT context FROM contexts WHERE key=? AND signature=?', (context_key(path, text), signature)).fetchone()
        return row[0] if row else ''

    def _search_text(self, con, path, kind, text, loc, signature):
        if kind == 'file':
            return text
        return enriched(self.header(path, kind, loc), self._context(con, path, text, signature), text)

    def _refresh_search_text(self, con):
        """Rewrite what search sees for every passage (after the header format or the context setting
        changed); passages whose search text changed lose their vector and are embedded again."""
        signature = self.context_signature()
        changed = 0
        rows = list(con.execute('SELECT s.id, s.path, s.kind, s.text, s.location, f.text FROM segments s JOIN fts f ON f.rowid = s.id'))
        with con:
            for segment_id, path, kind, text, location, old in rows:
                new = self._search_text(con, path, kind, text, json.loads(location or '{}'), signature)
                if new != old:
                    con.execute('UPDATE fts SET text=? WHERE rowid=?', (new, segment_id))
                    con.execute('DELETE FROM vectors WHERE segment_id=?', (segment_id,))
                    changed += 1
            if changed:
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))
        return changed

    # ---------------------------------------------------------------- status
    def status(self):
        con = connect(self.db_path)
        try:
            counts = dict(con.execute('SELECT status, count(*) FROM files GROUP BY status'))
            meta = dict(con.execute('SELECT key,value FROM meta'))
            segments = con.execute('SELECT count(*) FROM segments').fetchone()[0]
            vectors = con.execute('SELECT count(*) FROM vectors').fetchone()[0]
            described, describe_seconds = con.execute("SELECT count(*), avg(seconds) FROM descriptions WHERE status='ok'").fetchone()
        finally:
            con.close()
        roots = self.roots()
        return {'folders': len(roots), 'missing': [str(r) for r in roots if not r.is_dir()],
                'root_exists': any(r.is_dir() for r in roots), 'initialized': bool(meta.get('last_indexed')),
                'files': counts.get('ok', 0), 'pending': counts.get('pending', 0),
                'errors': counts.get('error', 0), 'unsupported': counts.get('unsupported', 0),
                'segments': segments, 'vectors': vectors, 'last_indexed': meta.get('last_indexed'),
                'described': described, 'describe_seconds': round(describe_seconds or 0, 2),
                'embed_model': meta.get('embed_signature'), 'contexts': self._context_count()}

    def _context_count(self):
        signature = self.context_signature()
        if not signature:
            return 0
        con = connect(self.db_path)
        try:
            return con.execute('SELECT count(*) FROM contexts WHERE signature=?', (signature,)).fetchone()[0]
        finally:
            con.close()

    def folder_stats(self):
        """{root: {'files': searchable, 'skipped': unsupported or failed, 'pending': text not read yet}} for each
        tracked folder. Pending files are findable by name, so they count among the searchable ones too."""
        con = connect(self.db_path)
        try:
            rows = list(con.execute('SELECT root, status, count(*) FROM files GROUP BY root, status'))
        finally:
            con.close()
        stats = {str(root): {'files': 0, 'skipped': 0, 'pending': 0} for root in self.roots()}
        for root, status, count in rows:
            if root in stats:
                stats[root]['files' if status in ('ok', 'pending') else 'skipped'] += count
                if status == 'pending':
                    stats[root]['pending'] += count
        return stats

    def problems(self, limit=50):
        con = connect(self.db_path)
        try:
            return [{'path': p, 'status': s, 'error': e} for p, s, e in con.execute(
                "SELECT path,status,error FROM files WHERE status='error' ORDER BY path LIMIT ?", (limit,))]
        finally:
            con.close()

    def pending_changes(self, cap=20000):
        """Files on disk the index hasn't caught up with (bounded, read-only). Files whose text is still to be
        read (an indexing run stopped after their names) count as changed."""
        con = connect(self.db_path)
        try:
            known = {p: (m, s) if status != 'pending' else None for p, m, s, status in con.execute('SELECT path,mtime_ns,size,status FROM files')}
        finally:
            con.close()
        new = changed = seen = 0
        present = set()
        for root in self.roots():
            if not root.is_dir():
                continue
            for path, st in discover(root):
                seen += 1
                present.add(path)
                if path not in known:
                    new += 1
                elif known[path] != (st.st_mtime_ns, st.st_size):
                    changed += 1
                if seen >= cap:
                    return {'new': new, 'changed': changed, 'removed': 0}
        return {'new': new, 'changed': changed, 'removed': len(set(known) - present)}

    def root_file_count(self):
        con = connect(self.db_path)
        try:
            return con.execute('SELECT count(*) FROM files').fetchone()[0]
        finally:
            con.close()

    # -------------------------------------------------------------- indexing
    def rebuild(self, progress=lambda state: None, embedder=None, vision=None):
        return self.update(progress, embedder, force=True, vision=vision)

    def update(self, progress=lambda state: None, embedder=None, force=False, vision=None, roots=None, background=False):
        """Bring the index up to date with the tracked folders (or just ``roots``). Returns a summary.

        Runs in layers, each committed as it completes (see the module notes): names, text, meaning, OCR (on
        worker threads from right after the names), picture vectors, PDF tables, then enrichment. ``background``
        (automatic runs): a smaller thread budget, and on battery only names and text are brought up to date."""
        if not self._lock.acquire(blocking=False):
            raise IndexBusy('Indexing is already running')
        self.cancel.clear()
        try:
            con = connect(self.db_path)
            try:
                return self._update(con, progress, embedder, force, roots, vision, background)
            finally:
                self._pause_check = None
                con.close()
        finally:
            self._lock.release()

    def thread_budget(self, background=False):
        """CPU threads indexing may use: setting index_threads, else all usable CPUs but two (a quarter of them
        in the background)."""
        try:
            chosen = int(self.settings.get('index_threads') or 0)
        except (TypeError, ValueError):
            chosen = 0
        if chosen > 0:
            return chosen
        cpus = power.usable_cpus()
        return max(1, cpus // 4) if background else max(1, cpus - 2)

    def _stopping(self):
        """True when the run should stop: cancelled, or (background on battery) paused."""
        if self.cancel.is_set():
            return True
        check = self._pause_check
        return bool(check and check())

    def _update(self, con, progress, embedder, force, only, vision=None, background=False):
        started = time.time()
        summary = {'found': 0, 'changed': 0, 'unchanged': 0, 'removed': 0, 'indexed': 0, 'errors': 0, 'unsupported': 0,
                   'embedded': 0, 'described': 0, 'warnings': [], 'seconds_to_names': None, 'seconds_to_text': None,
                   'seconds_to_core': None, 'ocr_device': 'off', 'ocr_read': 0, 'pictures_embedded': 0, 'cache_hits': 0,
                   'paused': None}
        run = _Run(self, con, progress, summary)
        tracked = self.roots()
        tracked_keys = {str(r) for r in tracked}
        # Files of folders that are no longer tracked leave the index.
        stale = [p for p, root in con.execute('SELECT path, root FROM files') if root not in tracked_keys]
        if force:
            stale = [p for (p,) in con.execute('SELECT path FROM files')]
            with con:
                con.execute('DELETE FROM media_cache')  # a rebuild reads everything again
        self._delete_files(con, stale)
        summary['removed'] += len(stale) if not force else 0
        ocr_on = bool(self.settings.get('ocr_enabled', True))
        interval = max(1, int(self.settings.get('video_interval') or 10))
        self._interval = interval
        meta = dict(con.execute('SELECT key,value FROM meta'))
        self._ocr_switch(con, ocr_on, meta)
        search_signature = self.search_signature()
        reread_documents = (meta.get('search_signature') or '').split('|')[0] != ENRICH_VERSION
        roots = [r for r in tracked if only is None or any(paths.canonical(r) == paths.canonical(o) for o in only)]

        def battery_pause():
            if summary['paused']:
                return True
            if power.on_battery():
                summary['paused'] = 'battery'
                summary['warnings'].append('On battery: text is searchable; the rest of indexing waits for mains power')
                return True
            return False
        self._pause_check = battery_pause if background and not self.settings.get('index_on_battery') else None

        # Layer 1: names. Every new file is findable by its name, in one transaction per folder.
        todo = []
        for root in roots:
            if self.cancel.is_set():
                break
            if not root.is_dir():
                summary['warnings'].append(f'{root.name} is missing')
                continue
            run.emit({'phase': 'scanning', 'done': 0, 'total': 0, 'root': str(root)})
            found = list(discover(root))
            known = {p: (m, s, status) for p, m, s, status in con.execute('SELECT path,mtime_ns,size,status FROM files WHERE root=?', (str(root),))}
            present = {p for p, _ in found}
            removed = [p for p in known if p not in present]
            self._delete_files(con, removed)
            new = [(p, st) for p, st in found if p not in known]
            self._add_names(con, root, new, summary)
            waiting = []
            for path, st in found:
                status = self._needs_text(path, st, known.get(path), reread_documents)
                if status:
                    waiting.append((root, path, st))
            summary['found'] += len(found)
            summary['changed'] += len(new) + sum(1 for _, p, _ in waiting if p in known)
            summary['unchanged'] += len(found) - len(new) - sum(1 for _, p, _ in waiting if p in known)
            summary['removed'] += len(removed)
            todo += waiting
            run.refresh()
            run.emit({'phase': 'names', 'done': len(found), 'total': len(found), 'root': str(root)})
        if not self.cancel.is_set():
            summary['seconds_to_names'] = round(time.time() - started, 2)

        # Layer 4 starts now: OCR on worker threads while the update thread reads text.
        ocr_runner = None
        if ocr_on:
            summary['ocr_device'] = ocr.configure(self.settings.get('ocr_device', 'auto'), self.thread_budget(background))
            if not self.cancel.is_set() and not self._stopping():
                cached = set(con.execute("SELECT hash, signature FROM media_cache WHERE layer='ocr'"))
                ocr_runner = _OcrRunner(ocr.engine_count(), interval, self.cancel, cached)
                waiting = {path for _, path, _ in todo}
                self._queue_ocr(con, ocr_runner, {str(r) for r in roots}, waiting)
        run.ocr = ocr_runner
        try:
            # Layer 2: text. Text-like files first, then Office files, then PDFs; smaller files first in each.
            todo.sort(key=lambda item: (_text_order(item[1]), item[2].st_size))
            for number, (root, path, st) in enumerate(todo):
                if self.cancel.is_set():
                    summary['warnings'].append('Stopped early; finished files were kept')
                    break
                if not any(paths.canonical(r) == paths.canonical(root) for r in self.roots()):
                    continue  # untracked while indexing: remove_root cleans up what was written
                run.emit({'phase': 'indexing', 'done': number, 'total': len(todo), 'current': path, 'root': str(root)})
                self._read_text(con, root, path, st, ocr_on, run)
                run.layers['text'][0] += 1
                run.apply_ocr()
            run.emit({'phase': 'indexing', 'done': len(todo), 'total': len(todo)})
            if not self.cancel.is_set():
                summary['seconds_to_text'] = round(time.time() - started, 2)
            if meta.get('search_signature') != search_signature and not self.cancel.is_set():
                self._refresh_search_text(con)
            if not self._stopping():
                self._core_layers(con, run, embedder, ocr_on)
            if not self._stopping():
                summary['seconds_to_core'] = round(time.time() - started, 2)
            # Layer 7: enrichment, timed separately.
            if vision is not None and not self._stopping() and ai.vision_background(self.settings):
                self._describe_missing(con, vision, embedder, run.emit, summary)
            writer = ai.Contextualizer.from_settings(self.settings)
            if writer is not None and not self._stopping():
                self._contextualize_missing(con, writer, embedder, run.emit, summary)
        finally:
            if ocr_runner is not None:
                ocr_runner.close()
        with con:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_indexed', time.strftime('%Y-%m-%d %H:%M')))
            if not self.cancel.is_set():
                if only is None or not reread_documents:  # documents of every folder were read with the new headers
                    con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('search_signature', search_signature))
                con.execute('DELETE FROM media_cache WHERE used_at < ?', (time.time() - CACHE_DAYS * 86400,))
        summary['seconds'] = round(time.time() - started, 2)
        run.refresh()
        run.emit({'phase': 'done', 'done': 1, 'total': 1})
        return summary

    def _core_layers(self, con, run, embedder, ocr_on):
        """Layers 3-6: meaning, the rest of OCR, picture vectors and PDF tables."""
        summary = run.summary
        run.refresh()
        if embedder is not None:
            summary['embedded'] += self._embed_missing(con, embedder, run.emit, summary, between=run.apply_ocr)
        if run.ocr is not None and not self._stopping():
            run.refresh()
            while run.ocr.outstanding and not self._stopping():
                run.emit({'phase': 'reading pictures', 'done': run.ocr.total - run.ocr.outstanding, 'total': run.ocr.total})
                run.apply_ocr(wait=0.5)
            run.emit({'phase': 'reading pictures', 'done': run.ocr.total - run.ocr.outstanding, 'total': run.ocr.total})
            if embedder is not None and not self._stopping():
                summary['embedded'] += self._embed_missing(con, embedder, run.quiet('reading pictures'), summary)
        if embedder is not None and not self._stopping() and self.settings.get('picture_search', True):
            run.refresh()
            self._embed_pictures(con, embedder, run)
        # PDFs are searchable by now (their text was read quickly); their tables are read row by row last.
        if not self._stopping() and self._read_pdf_tables(con, ocr_on, run.emit, summary) and embedder is not None:
            summary['embedded'] += self._embed_missing(con, embedder, run.quiet('reading tables'), summary)

    @staticmethod
    def _needs_text(path, st, known, reread_documents):
        """Whether layer 2 must (re)read a file: changed, still pending, read before the chunk headers, or a
        picture / video an older version could not index."""
        if known is None:
            return extract.unsupported_reason(path) is None and Path(path).suffix.lower() not in MEDIA
        mtime, size, status = known
        suffix = Path(path).suffix.lower()
        if (mtime, size) != (st.st_mtime_ns, st.st_size) or status == 'pending':
            return True
        if reread_documents and suffix in REREAD_FOR_ENRICH:
            return True  # read again for the headings and titles the chunk headers use
        return suffix in MEDIA and status != 'ok'

    def _add_names(self, con, root, new, summary):
        """Layer 1 for new files: a files row and one name-only passage each. Documents wait as 'pending' for
        layer 2; for a picture or video the name is its text layer."""
        if not new:
            return
        now = time.time()
        with con:
            for path, st in new:
                reason = extract.unsupported_reason(path)
                if reason:
                    con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)',
                                (path, str(root), st.st_mtime_ns, st.st_size, 'unsupported', reason, 0, now))
                    summary['unsupported'] += 1
                    continue
                media = Path(path).suffix.lower() in MEDIA
                kind, text, loc = _name_only(path)
                cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)', (path, 0, kind, text, json.dumps(loc)))
                con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)', (cursor.lastrowid, text, name_terms(path)))
                con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)',
                            (path, str(root), st.st_mtime_ns, st.st_size, 'ok' if media else 'pending', '', 1, now))
                if media:
                    summary['indexed'] += 1
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

    def _read_text(self, con, root, path, st, ocr_on, run):
        """Layer 2 for one file. A changed picture or video is reset to its name (its OCR, picture vectors and
        description are redone); a PDF's pages without text are left for OCR."""
        summary = run.summary
        suffix = Path(path).suffix.lower()
        if suffix in MEDIA:
            self._write(con, root, path, st, 'ok', '', [_name_only(path)], clear=True)
            summary['indexed'] += 1
            if run.ocr is not None:
                run.ocr.submit(path)
            return
        status, error, passages = 'ok', '', []
        deferred = [] if suffix == '.pdf' else None
        try:
            passages = extract.extract(path, ocr_on, self._interval, None, self.cancel, pdf_tables=False, deferred=deferred)
            if not passages and deferred:
                passages = [_name_only(path)]  # a scan: findable by name until OCR has read it
            elif not passages:
                status, error = 'error', 'No readable text'
        except extract.Unsupported as reason:
            status, error = 'unsupported', str(reason)
        except Exception as exc:  # a bad file never stops the run
            status, error = 'error', (str(exc) or type(exc).__name__)[:300]
            log.warning('Could not index %s: %s', path, error)
        layers = []
        if status == 'ok' and deferred:
            detail = json.dumps({'pages': deferred})
            layers.append(('picture', '', 'pending', '', detail, 0))
            if ocr_on:
                layers.append(('ocr', ocr_signature(path, self._interval), 'pending', '', detail, 0))
        self._write(con, root, path, st, status, error, passages, clear=True, pdf_tables=False if suffix == '.pdf' else None, layers=layers)
        summary['indexed' if status == 'ok' else 'errors' if status == 'error' else 'unsupported'] += 1
        if run.ocr is not None and status == 'ok' and deferred:
            run.ocr.submit(path)

    def _write(self, con, root, path, st, status, error, passages, clear, pdf_tables=None, layers=()):
        """Store a file's text passages; the one writer for layer 2, OCR results and PDF tables.

        ``clear`` (layer 2 on a changed file): everything about the file goes first, its pictures, description
        and layer rows too. Otherwise only its text passages are replaced, and its picture vectors and AI
        description stay. Either way, passages that read the same as before keep their vectors: an edited file
        (or one read again by a newer reader) only re-embeds what changed."""
        with con:
            kept = {text: (dim, vector) for text, dim, vector in con.execute(
                'SELECT f.text, v.dim, v.vector FROM segments s JOIN fts f ON f.rowid = s.id '
                "JOIN vectors v ON v.segment_id = s.id WHERE s.path=? AND s.kind NOT IN ('picture', 'vision')", (path,))}
            if clear:
                self._delete_passages(con, [path])
            else:
                self._delete_text_passages(con, path)
            name = name_terms(path)
            context_signature = self.context_signature()
            for order, (kind, text, loc) in enumerate(passages):
                cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)',
                                     (path, order, kind, text, json.dumps(loc)))
                search_text = self._search_text(con, path, kind, text, loc, context_signature)
                con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)', (cursor.lastrowid, search_text, name))
                if search_text in kept:
                    con.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (cursor.lastrowid, *kept[search_text]))
            con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)',
                        (path, str(root), st.st_mtime_ns, st.st_size, status, error, len(passages), time.time()))
            if pdf_tables is True:
                con.execute('INSERT OR REPLACE INTO pdf_tables VALUES(?,?)', (path, PDF_READER))
            elif pdf_tables is False:
                con.execute('DELETE FROM pdf_tables WHERE path=?', (path,))
            for layer, signature, layer_status, layer_error, detail, seconds in layers:
                con.execute('INSERT OR REPLACE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                            (path, layer, signature, layer_status, layer_error, detail, round(seconds or 0, 3), time.time()))

    def _delete_text_passages(self, con, path):
        """Remove a file's text passages (everything but its picture vectors and AI description)."""
        ids = [row[0] for row in con.execute("SELECT id FROM segments WHERE path=? AND kind NOT IN ('picture', 'vision')", (path,))]
        self._delete_segments(con, ids)

    def _delete_segments(self, con, ids):
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            marks = ','.join('?' * len(batch))
            con.execute(f'DELETE FROM fts WHERE rowid IN ({marks})', batch)
            con.execute(f'DELETE FROM vectors WHERE segment_id IN ({marks})', batch)
            con.execute(f'DELETE FROM segments WHERE id IN ({marks})', batch)
        if ids:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

    def _ocr_switch(self, con, ocr_on, meta):
        """OCR turned off: passages read by OCR leave the index (a picture keeps its name). Turned back on:
        scanned PDF pages are queued for OCR again."""
        previous, now = meta.get('ocr_enabled'), '1' if ocr_on else '0'
        if previous == now:
            return
        with con:
            if not ocr_on and previous == '1':
                rows = list(con.execute("SELECT id, path FROM segments WHERE json_extract(location, '$.ocr')"))
                self._delete_segments(con, [segment_id for segment_id, _ in rows])
                con.execute("DELETE FROM layers WHERE layer='ocr'")
                for path in {path for _, path in rows}:
                    left = con.execute("SELECT count(*) FROM segments WHERE path=? AND kind NOT IN ('picture', 'vision')", (path,)).fetchone()[0]
                    if not left:
                        kind, text, loc = _name_only(path)
                        cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)', (path, 0, kind, text, json.dumps(loc)))
                        con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)', (cursor.lastrowid, text, name_terms(path)))
                    con.execute('UPDATE files SET passages=(SELECT count(*) FROM segments WHERE path=? AND ord >= 0) WHERE path=?', (path, path))
            elif ocr_on and previous == '0':
                for path, detail in list(con.execute("SELECT path, detail FROM layers WHERE layer='picture' AND detail IS NOT NULL")):
                    con.execute('INSERT OR IGNORE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                                (path, 'ocr', ocr_signature(path, self._interval), 'pending', '', detail, 0, time.time()))
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('ocr_enabled', now))

    def _queue_ocr(self, con, runner, roots, skip):
        """Queue every picture, video and scanned PDF of ``roots`` whose OCR is missing or out of date."""
        for path, root, signature, status in list(con.execute(
                "SELECT f.path, f.root, l.signature, l.status FROM files f LEFT JOIN layers l ON l.path = f.path AND l.layer = 'ocr' "
                "WHERE f.status = 'ok'")):
            if root not in roots or path in skip:
                continue
            suffix = Path(path).suffix.lower()
            if suffix in MEDIA:
                if status is None or status == 'pending' or signature != ocr_signature(path, self._interval):
                    runner.submit(path)
            elif suffix == '.pdf' and status == 'pending':
                runner.submit(path)

    def _apply_ocr(self, con, run, result):
        """Store one OCR result (from a worker): its text passages replace the file's, its picture vectors and
        description stay. Dropped when the file changed since it was read."""
        summary, path = run.summary, result['path']
        row = con.execute('SELECT root, mtime_ns, size, status FROM files WHERE path=?', (path,)).fetchone()
        if row is None or row[3] != 'ok' or result.get('cancelled') or 'stat' not in result:
            return
        if result.get('changed') or (row[1], row[2]) != result['stat']:
            log.info('Dropped the OCR result of %s: the file changed while it was read', path)
            return
        signature = ocr_signature(path, self._interval)
        cache_signature = _ocr_cache_signature(path, self._interval)
        run.hashes[path] = (result['stat'], result['hash'])
        previous = con.execute("SELECT detail FROM layers WHERE path=? AND layer='ocr'", (path,)).fetchone()
        detail = previous[0] if previous else None
        if result.get('error'):
            with con:
                con.execute('INSERT OR REPLACE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                            (path, 'ocr', signature, 'error', result['error'][:300], detail, 0, time.time()))
            return
        if result.get('cached'):
            stored = con.execute("SELECT payload FROM media_cache WHERE hash=? AND layer='ocr' AND signature=?",
                                 (result['hash'], cache_signature)).fetchone()
            if stored is None:  # removed meanwhile: read it after all
                run.ocr.cached.discard((result['hash'], cache_signature))
                run.ocr.submit(path, again=True)
                return
            passages = [tuple(p) for p in json.loads(stored[0])]
            with con:
                con.execute("UPDATE media_cache SET used_at=? WHERE hash=? AND layer='ocr' AND signature=?",
                            (time.time(), result['hash'], cache_signature))
            summary['cache_hits'] += 1
        else:
            passages = result['passages']
            with con:
                con.execute('INSERT OR REPLACE INTO media_cache VALUES(?,?,?,?,?)',
                            (result['hash'], 'ocr', cache_signature, json.dumps(passages).encode('utf-8'), time.time()))
            summary['ocr_read'] += 1
        pdf = Path(path).suffix.lower() == '.pdf'
        status, error = 'ok', ''
        if not passages:
            if pdf:
                status, error = 'error', 'No readable text'
            else:
                passages = [_name_only(path)]
        self._write(con, row[0], path, _Stat(row[1], row[2]), status, error, passages, clear=False,
                    pdf_tables=True if pdf else None,
                    layers=[('ocr', signature, 'ok', '', detail, result.get('seconds', 0))])
        if pdf and status == 'error':
            summary['errors'] += 1

    def _read_pdf_tables(self, con, ocr_on, progress, summary):
        """Read the tables of PDFs indexed with the quick reader (or an older table reader). True if any changed.
        A PDF still waiting for OCR is left to its OCR read, which reads its tables too."""
        tracked = {str(r) for r in self.roots()}
        todo = [(path, root, ocr_status) for path, root, ocr_status in con.execute(
            "SELECT f.path, f.root, o.status FROM files f LEFT JOIN pdf_tables t ON t.path = f.path "
            "LEFT JOIN layers o ON o.path = f.path AND o.layer = 'ocr' WHERE f.status='ok' "
            "AND lower(f.path) LIKE '%.pdf' AND (t.reader IS NULL OR t.reader != ?)", (PDF_READER,)) if root in tracked]
        todo = [item for item in todo if not (ocr_on and item[2] == 'pending')]
        changed = False
        for number, (path, root, ocr_status) in enumerate(todo):
            if self._stopping():
                break
            progress({'phase': 'reading tables', 'done': number, 'total': len(todo), 'current': path, 'root': root})
            try:
                import pymupdf
                with pymupdf.open(path) as document:
                    has_tables = bool(extract.table_pages(document))
                st = os.stat(path)
                if has_tables:
                    inline = ocr_on and ocr_status == 'ok'  # pages read by OCR before: read them again with it
                    passages = extract.extract(path, inline, self._interval, None, self.cancel, pdf_tables=True,
                                               deferred=None if inline else [])
            except Exception as error:  # gone, locked or unreadable now: left for the next update to try again
                log.warning('Could not read the tables of %s: %s', path, error)
                continue
            if has_tables:
                self._write(con, Path(root), path, st, 'ok', '', passages or [_name_only(path)], clear=False, pdf_tables=True)
                changed = True
            else:
                with con:
                    con.execute('INSERT OR REPLACE INTO pdf_tables VALUES(?,?)', (path, PDF_READER))
        return changed

    # ------------------------------------------------------------- pictures
    def _picture_support(self, con, embedder):
        """Whether ``embedder`` embeds pictures (probed once per process; remembered in the index for layer_status)."""
        probe = getattr(embedder, 'supports_pictures', None)
        if probe is None or not hasattr(embedder, 'embed_pictures'):
            return False
        try:
            supported = bool(probe())
        except Exception:
            supported = False
        with con:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('picture_support', f'{embedder.signature()}:{int(supported)}'))
        return supported

    def _embed_pictures(self, con, embedder, run):
        """Layer 5: a vector for every picture, up to 8 keyframes per video and each scanned PDF page, from the
        embedding model itself, so a text query finds a picture by what it shows."""
        summary = run.summary
        if not hasattr(embedder, 'embed_pictures'):
            return
        if not self._picture_support(con, embedder):
            summary['warnings'].append('Search by what pictures show is unavailable: the embedding model does not read pictures')
            return
        signature = picture_signature(embedder)
        tracked = {str(r) for r in self.roots()}
        todo = []
        for path, root, mtime, size, done, status, detail in list(con.execute(
                "SELECT f.path, f.root, f.mtime_ns, f.size, p.signature, p.status, p.detail FROM files f "
                "LEFT JOIN layers p ON p.path = f.path AND p.layer = 'picture' WHERE f.status = 'ok'")):
            if root not in tracked:
                continue
            suffix = Path(path).suffix.lower()
            if suffix not in MEDIA and not (suffix == '.pdf' and detail):
                continue
            if done == signature and status != 'pending':
                continue
            todo.append((path, (mtime, size), json.loads(detail)['pages'] if detail else None))
        pending, images = [], 0
        run.layers['pictures'][1] = max(run.layers['pictures'][1], run.layers['pictures'][0] + len(todo))

        def flush():
            nonlocal pending, images
            if not pending:
                return True
            batch, pending, images = pending, [], 0
            try:
                vectors = embedder.embed_pictures([image for _, _, _, frames in batch for _, image in frames])
            except Exception as error:
                summary['warnings'].append(f'Search by what pictures show paused: {error}')
                return False
            for path, stat, digest, frames in batch:
                found = [(loc, vectors.pop(0)) for loc, _ in frames]
                self._store_pictures(con, path, signature, found, digest)
                summary['pictures_embedded'] += len(found)
                run.layers['pictures'][0] += 1
            return True

        for number, (path, stat, pages) in enumerate(todo):
            if self._stopping():
                break
            run.emit({'phase': 'embedding pictures', 'done': number, 'total': len(todo), 'current': path})
            try:
                digest = run.file_hash(path, stat)
                stored = con.execute("SELECT payload FROM media_cache WHERE hash=? AND layer='picture' AND signature=?",
                                     (digest, signature)).fetchone()
                if stored is not None:
                    self._store_pictures(con, path, signature, _unpack_pictures(stored[0]), None)
                    with con:
                        con.execute("UPDATE media_cache SET used_at=? WHERE hash=? AND layer='picture' AND signature=?", (time.time(), digest, signature))
                    summary['cache_hits'] += 1
                    run.layers['pictures'][0] += 1
                    continue
                frames = _picture_frames(path, pages)
            except Exception as error:  # unreadable now: recorded, tried again when the file changes
                with con:
                    con.execute('INSERT OR REPLACE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                                (path, 'picture', signature, 'error', (str(error) or type(error).__name__)[:300],
                                 json.dumps({'pages': pages}) if pages else None, 0, time.time()))
                continue
            pending.append((path, stat, digest, frames))
            images += len(frames)
            if images >= ai.PICTURE_BATCH and not flush():
                return
        if not self._stopping():
            flush()
        run.emit({'phase': 'embedding pictures', 'done': len(todo), 'total': len(todo)})

    def _store_pictures(self, con, path, signature, found, digest):
        """Replace a file's picture vectors with ``found`` [(location, vector)]; cache them under ``digest``."""
        detail = con.execute("SELECT detail FROM layers WHERE path=? AND layer='picture'", (path,)).fetchone()
        with con:
            old = [row[0] for row in con.execute("SELECT id FROM segments WHERE path=? AND kind='picture'", (path,))]
            self._delete_segments(con, old)
            for number, (loc, vector) in enumerate(found):
                array = np.array(vector, dtype=np.float32)  # a copy: cached vectors are read-only views
                array /= max(float(np.linalg.norm(array)), 1e-12)
                cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)',
                                     (path, -2 - number, 'picture', '', json.dumps(loc)))
                con.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (cursor.lastrowid, array.size, array.tobytes()))
            con.execute('INSERT OR REPLACE INTO layers VALUES(?,?,?,?,?,?,?,?)',
                        (path, 'picture', signature, 'ok', '', detail[0] if detail else None, 0, time.time()))
            if digest is not None:
                con.execute('INSERT OR REPLACE INTO media_cache VALUES(?,?,?,?,?)',
                            (digest, 'picture', signature, _pack_pictures(found), time.time()))
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

    # ------------------------------------------------------------- layer status
    def layer_status(self):
        """{'names', 'text', 'meaning', 'pictures'}: [done, total] for each layer, read from the index."""
        con = connect(self.db_path)
        try:
            return self._layer_counts(con)
        finally:
            con.close()

    def _layer_counts(self, con):
        total, read = con.execute("SELECT count(*), count(CASE WHEN status != 'pending' THEN 1 END) FROM files").fetchone()
        counts = {'names': [total, total], 'text': [read, total], 'meaning': [0, 0], 'pictures': [0, 0]}
        embedder = ai.Embedder.from_settings(self.settings)
        if embedder is not None:
            passages, embedded = con.execute("SELECT count(*), count(v.segment_id) FROM segments s "
                                             "LEFT JOIN vectors v ON v.segment_id = s.id WHERE s.kind != 'picture'").fetchone()
            counts['meaning'] = [embedded, passages]
        media = con.execute(f"SELECT count(*) FROM files f WHERE f.status = 'ok' AND {MEDIA_SQL}").fetchone()[0]
        required = done = 0
        layers = []
        if self.settings.get('ocr_enabled', True):
            layers.append(('ocr', None))
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key IN ('picture_support', 'embed_signature')"))
        if embedder is not None and self.settings.get('picture_search', True) and meta.get('picture_support') != f'{embedder.signature()}:0':
            layers.append(('picture', picture_signature(embedder)))
        for layer, signature in layers:
            pdfs, finished = con.execute(
                "SELECT count(CASE WHEN lower(f.path) LIKE '%.pdf' THEN 1 END), "
                "count(CASE WHEN l.status IN ('ok', 'error') AND (? IS NULL OR l.signature = ?) THEN 1 END) "
                "FROM layers l JOIN files f ON f.path = l.path WHERE l.layer = ? AND f.status = 'ok'", (signature, signature, layer)).fetchone()
            required += media + pdfs
            done += finished
        counts['pictures'] = [min(done, required), required]
        return counts

    # ------------------------------------------------------------- deleting
    def remove_root(self, root):
        """Forget every file of ``root`` (after it is untracked)."""
        key = str(Path(os.path.realpath(root)))
        with self._lock:
            con = connect(self.db_path)
            try:
                self._delete_files(con, [p for (p,) in con.execute('SELECT path FROM files WHERE root=?', (key,))])
            finally:
                con.close()

    def _delete_files(self, con, files):
        if not files:
            return
        with con:
            self._delete_passages(con, files)
            for start in range(0, len(files), 500):
                batch = files[start:start + 500]
                con.execute(f'DELETE FROM files WHERE path IN ({",".join("?" * len(batch))})', batch)

    def _delete_passages(self, con, files):
        for path in files:
            ids = [row[0] for row in con.execute('SELECT id FROM segments WHERE path=?', (path,))]
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                marks = ','.join('?' * len(batch))
                con.execute(f'DELETE FROM fts WHERE rowid IN ({marks})', batch)
                con.execute(f'DELETE FROM vectors WHERE segment_id IN ({marks})', batch)
            con.execute('DELETE FROM segments WHERE path=?', (path,))
            con.execute('DELETE FROM descriptions WHERE path=?', (path,))
            con.execute('DELETE FROM pdf_tables WHERE path=?', (path,))
            con.execute('DELETE FROM layers WHERE path=?', (path,))
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

    # ------------------------------------------------------------- captions
    def _describe_missing(self, con, vision, embedder, progress, summary):
        """Describe pictures (and one representative frame per video) that have no description from
        this model yet. Runs once per file version: re-indexing a changed file clears its description."""
        signature = vision.signature()
        media = tuple(IMAGE_SUFFIXES | VIDEO_SUFFIXES)
        rows = [(p, root) for p, root in con.execute(
            "SELECT f.path, f.root FROM files f LEFT JOIN descriptions d ON d.path = f.path "
            "WHERE f.status = 'ok' AND (d.signature IS NULL OR d.signature != ?)", (signature,)) if p.lower().endswith(media)]
        tracked = {str(r) for r in self.roots()}
        rows = [(p, root) for p, root in rows if root in tracked]
        pending_embeds = 0
        for number, (path, root) in enumerate(rows):
            if self._stopping():
                break
            progress({'phase': 'describing', 'done': number, 'total': len(rows), 'current': path, 'root': root})
            try:
                described = self._describe_one(con, path, vision, signature)
            except ai.ProviderError as error:
                summary['warnings'].append(f'Picture descriptions paused: {error}')
                break  # Ollama or the model is unavailable: try again on the next run
            if described is None:
                continue
            summary['described'] += 1
            pending_embeds += 1
            if embedder is not None and pending_embeds >= DESCRIBE_EMBED_EVERY:
                summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)
                pending_embeds = 0
        if embedder is not None and pending_embeds:
            summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)

    def _describe_one(self, con, path, vision, signature):
        """Describe one picture or video and store its 'vision' passage: (text, segment id), or None when this
        file could not be described (recorded, so it isn't tried again until it changes). Raises ProviderError
        when Ollama or the model is unavailable."""
        began = time.time()
        try:
            image, seconds = self._picture(path)
            described = vision.describe(image)
        except ai.ProviderError as error:
            if 'Cannot reach' in str(error) or 'not installed' in str(error):
                raise
            self._record_description(con, path, signature, 'error', str(error)[:300], time.time() - began)
            return None
        except Exception as error:  # an unreadable picture never stops the run
            self._record_description(con, path, signature, 'error', (str(error) or type(error).__name__)[:300], time.time() - began)
            return None
        location = dict(ai=True, model=vision.model, **described)
        if seconds is not None:
            location['seconds'] = seconds
        text = '\n'.join(part for part in (described['caption'], described['description'],
                                            'Tags: ' + ', '.join(described['tags']) if described['tags'] else '') if part)
        with con:
            old = [row[0] for row in con.execute("SELECT id FROM segments WHERE path=? AND kind='vision'", (path,))]
            self._delete_segments(con, old)
            cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)',
                                 (path, -1, 'vision', text, json.dumps(location)))
            con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)',
                        (cursor.lastrowid, self._search_text(con, path, 'vision', text, location, None), name_terms(path)))
        self._record_description(con, path, signature, 'ok', '', time.time() - began)
        return text, cursor.lastrowid

    def describe_now(self, path, vision, embedder=None):
        """Describe one picture or video now (lazy captions: when its preview opens) and return the description,
        or None. Embeds the new passage when ``embedder`` is given. Safe while update() runs (its own connection,
        not the indexing lock); two calls for the same file describe it once."""
        key = str(path)
        if Path(key).suffix.lower() not in MEDIA:
            return None
        with self._describe_lock:
            event = self._describing.get(key)
            owner = event is None
            if owner:
                event = self._describing[key] = threading.Event()
        if not owner:
            event.wait(300)
            return self._stored_description(key, vision.signature())
        try:
            con = connect(self.db_path)
            try:
                signature = vision.signature()
                existing = self._stored_description(key, signature, con)
                if existing is not None:
                    return existing
                row = con.execute('SELECT status FROM files WHERE path=?', (key,)).fetchone()
                if row is None or row[0] != 'ok' or self._place(key) is None:
                    return None
                try:
                    described = self._describe_one(con, key, vision, signature)
                except ai.ProviderError as error:
                    log.info('Could not describe %s now: %s', key, error)
                    return None
                if described is None:
                    return None
                if embedder is not None:
                    try:
                        self._embed_segments(con, embedder, [described[1]])
                    except Exception as error:  # the description is searchable by keyword already
                        log.info('Could not embed the description of %s: %s', key, error)
                return described[0]
            finally:
                con.close()
        finally:
            with self._describe_lock:
                self._describing.pop(key, None)
            event.set()

    def _stored_description(self, path, signature, con=None):
        own = con is None
        con = con or connect(self.db_path)
        try:
            row = con.execute("SELECT s.text FROM segments s JOIN descriptions d ON d.path = s.path WHERE s.path=? AND s.kind='vision' "
                              "AND d.signature=? AND d.status='ok'", (path, signature)).fetchone()
            return row[0] if row else None
        finally:
            if own:
                con.close()

    def _embed_segments(self, con, embedder, ids):
        """Embed just these passages (when the index's vectors are from this model)."""
        meta = dict(con.execute("SELECT key,value FROM meta WHERE key='embed_signature'"))
        if meta.get('embed_signature') != self.vector_signature(embedder) or not ids:
            return 0
        rows = list(con.execute('SELECT s.id, s.path, f.text FROM segments s JOIN fts f ON f.rowid = s.id '
                                'WHERE s.id IN (SELECT value FROM json_each(?))', (json.dumps(ids),)))
        if not rows:
            return 0
        vectors = embedder.embed_documents([text for _, _, text in rows], [Path(p).stem for _, p, _ in rows])
        with con:
            for (segment_id, _, _), vector in zip(rows, vectors):
                array = np.asarray(vector, dtype=np.float32)
                array /= max(float(np.linalg.norm(array)), 1e-12)
                con.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (segment_id, array.size, array.tobytes()))
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))
        return len(rows)

    @staticmethod
    def _picture(path):
        """(PIL image, seconds or None) to describe: the picture itself, or a frame a fifth into a video."""
        from PIL import Image
        import io
        if Path(path).suffix.lower() in VIDEO_SUFFIXES:
            duration = extract.video_duration(path)
            seconds = round(min(duration * 0.2, 30.0), 1)
            return Image.open(io.BytesIO(video_frame_png(path, seconds, width=896))), seconds
        with Image.open(path) as image:
            image.seek(0)
            from PIL import ImageOps
            return ImageOps.exif_transpose(image).copy(), None

    @staticmethod
    def _record_description(con, path, signature, status, error, seconds):
        with con:
            con.execute('INSERT OR REPLACE INTO descriptions VALUES(?,?,?,?,?,?)', (path, signature, status, error, round(seconds, 2), time.time()))

    def description_pending(self, path):
        """True while a picture is waiting for its AI description."""
        if not self.settings.get('vision_enabled') or Path(path).suffix.lower() not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
            return False
        signature = ai.Vision(self.settings).signature()
        con = connect(self.db_path)
        try:
            row = con.execute('SELECT signature FROM descriptions WHERE path=?', (str(path),)).fetchone()
        finally:
            con.close()
        return row is None or row[0] != signature

    # ------------------------------------------------------------- meaning
    def _embed_missing(self, con, embedder, progress, summary, between=None):
        """Embed passages that have no vector yet, in batches; re-embed everything after a model change.
        ``between`` runs after each batch (the update thread stores finished OCR results there)."""
        signature = self.vector_signature(embedder)
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('embed_signature') != signature:
            with con:
                con.execute('DELETE FROM vectors')
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('embed_signature', signature))
        # Embed what keyword search sees too: the passage with its header (and AI context, when on).
        rows = list(con.execute('SELECT s.id, s.path, f.text FROM segments s JOIN fts f ON f.rowid = s.id '
                                'LEFT JOIN vectors v ON v.segment_id=s.id WHERE v.segment_id IS NULL'))
        done = 0
        for start in range(0, len(rows), embedder.batch):
            if self._stopping():
                break
            batch = rows[start:start + embedder.batch]
            progress({'phase': 'embedding', 'done': done, 'total': len(rows), 'current': batch[0][1]})
            try:
                vectors = embedder.embed_documents([text for _, _, text in batch], [Path(p).stem for _, p, _ in batch])
            except Exception as exc:
                summary['warnings'].append(f'Search by meaning paused: {exc}')
                break
            with con:
                for (segment_id, _, _), vector in zip(batch, vectors):
                    array = np.asarray(vector, dtype=np.float32)
                    array /= max(float(np.linalg.norm(array)), 1e-12)
                    con.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (segment_id, array.size, array.tobytes()))
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))
            done += len(batch)
            if between is not None:
                between()
        return done

    def _contextualize_missing(self, con, writer, embedder, progress, summary):
        """Write a sentence or two situating each passage in its document (contextual retrieval), once
        per passage text and model; the context joins the passage's search text and it is re-embedded."""
        signature = writer.signature()
        tracked = {str(r) for r in self.roots()}
        marks = ','.join('?' * len(CONTEXT_KINDS))
        rows = list(con.execute(f"SELECT s.id, s.path, s.ord, s.kind, s.text, s.location FROM segments s JOIN files f ON f.path = s.path "
                                f"WHERE f.status='ok' AND s.kind IN ({marks}) ORDER BY s.path, s.ord", CONTEXT_KINDS))
        known = {key for (key,) in con.execute('SELECT key FROM contexts WHERE signature=?', (signature,))}
        todo = [row for row in rows if context_key(row[1], row[4]) not in known and self.root_of(row[1]) is not None
                and str(self.root_of(row[1])) in tracked]
        documents, pending = {}, 0
        for number, (segment_id, path, order, kind, text, location) in enumerate(todo):
            if self._stopping():
                break
            progress({'phase': 'adding context', 'done': number, 'total': len(todo), 'current': path})
            if path not in documents:
                documents[path] = [(o, t) for o, t in con.execute('SELECT ord, text FROM segments WHERE path=? AND ord >= 0 ORDER BY ord', (path,))]
            try:
                context = writer.situate(Path(path).name, _document_window(documents[path], order), text)
            except ai.ProviderError as error:
                summary['warnings'].append(f'Passage context paused: {error}')
                break
            loc = json.loads(location or '{}')
            with con:
                con.execute('INSERT OR REPLACE INTO contexts VALUES(?,?,?)', (context_key(path, text), signature, context))
                con.execute('UPDATE fts SET text=? WHERE rowid=?', (enriched(self.header(path, kind, loc), context, text), segment_id))
                con.execute('DELETE FROM vectors WHERE segment_id=?', (segment_id,))
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))
            summary['contextualized'] = summary.get('contextualized', 0) + 1
            pending += 1
            if embedder is not None and pending >= DESCRIBE_EMBED_EVERY:
                summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)
                pending = 0
        if embedder is not None and pending:
            summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)

    # ------------------------------------------------------------- retrieval
    def retrieve(self, query, embedder=None, pool=40, keep=30, path=None, band=0.12):
        """Candidate passages for answering ``query``: keyword (BM25) and meaning matches, ``pool`` of
        each, fused by reciprocal rank to the best ``keep``. No per-file limit; file-name-only entries
        are left out. Each: {'segment_id', 'path', 'ord', 'kind', 'text', 'loc', 'header', 'score', ...}."""
        query = (query or '').strip()
        if not query:
            return []
        con = self._reader()
        scope = self._scope_ids(con, path) if path else None  # a one-file chat searches only that file
        profile = query_profile(query)
        keyword = self._keyword(con, query, pool, scope)
        semantic, _ = self._semantic(con, query, pool, embedder, band, scope, pictures=False)  # answers need text
        ordered = self._fuse(keyword, semantic, self._phrases(con, profile, pool, scope), profile.semantic_weight)
        rows = self._rows(con, [segment_id for segment_id, _ in ordered],
                          "s.path, s.ord, s.kind, s.text, s.location", "f.status='ok'")
        target = paths.canonical(path) if path else None
        found = []
        for segment_id, info in ordered:
            row = rows.get(segment_id)
            if not row or row[2] in ('file', 'picture'):
                continue
            file, order, kind, text, location = row
            if target and paths.canonical(file) != target:
                continue
            if self._place(file) is None:
                continue
            loc = json.loads(location or '{}')
            found.append(dict(info, segment_id=segment_id, path=file, ord=order, kind=kind, text=text, loc=loc,
                              header=self.header(file, kind, loc)))
            if len(found) >= keep:
                break
        return found

    @staticmethod
    def _fuse(keyword, semantic, phrases=(), semantic_weight=1.0):
        """Reciprocal-rank fusion of the keyword and meaning lists: [(segment_id, info)] best first.
        ``phrases`` holds one ranked list per identifier in the query (passages containing it exactly); each
        identifier a passage contains lifts it above every passage that contains fewer."""
        fused = {}
        for channel, ranked, weight in (('keyword', keyword, 1.0), ('semantic', semantic, semantic_weight)):
            for rank, (segment_id, raw) in enumerate(ranked):
                item = fused.setdefault(segment_id, {'score': 0.0})
                item['score'] += weight / (RRF_K + rank + 1)
                item[channel + '_rank'] = rank + 1
                item[channel + '_score'] = float(raw)
        for ranked in phrases:
            for rank, (segment_id, _) in enumerate(ranked):
                item = fused.setdefault(segment_id, {'score': 0.0})
                item['score'] += PHRASE_BOOST + 1.0 / (RRF_K + rank + 1)
                item['exact_matches'] = item.get('exact_matches', 0) + 1
        return sorted(fused.items(), key=lambda kv: -kv[1]['score'])

    @staticmethod
    def _phrases(con, profile, pool, scope=None):
        """For each identifier or date in the query, the passages that contain it as a phrase (its pieces
        adjacent and in order, in any of its written forms), best BM25 first."""
        within = ' AND rowid IN (SELECT value FROM json_each(?))' if scope is not None else ''
        extra = (json.dumps(scope),) if scope is not None else ()
        sql = f'SELECT rowid, bm25(fts, 1.0, 2.5) FROM fts WHERE fts MATCH ?{within} ORDER BY bm25(fts, 1.0, 2.5) LIMIT ?'
        lists = []
        for number, forms in enumerate(profile.identifiers):
            phrases = ['"' + ' '.join(form) + '"' for form in forms]
            matches = [' OR '.join(phrases)]
            if number == profile.typing:  # being typed: its last piece may be a prefix; whole matches stay ahead (they are in both lists)
                matches.append(' OR '.join(phrase + ' *' for phrase in phrases))
            for match in matches:
                try:
                    lists.append([(rowid, -rank) for rowid, rank in con.execute(sql, (match, *extra, pool))])
                except sqlite3.OperationalError:
                    lists.append([])
        return lists

    @staticmethod
    def _rows(con, ids, columns, where=''):
        """{segment id: row of ``columns``} for many passages in one query (s = segments, f = files, x = fts)."""
        if not ids:
            return {}
        fts = ' LEFT JOIN fts x ON x.rowid = s.id' if 'x.' in columns else ''
        sql = (f'SELECT s.id, {columns} FROM segments s JOIN files f ON f.path = s.path{fts} '
               f'WHERE s.id IN (SELECT value FROM json_each(?)){" AND " + where if where else ""}')
        return {row[0]: row[1:] for row in con.execute(sql, (json.dumps(ids),))}

    def neighbours(self, path, order, radius=1):
        """Passages next to passage ``order`` of ``path`` (same file, readable text only), in order."""
        rows = self._reader().execute("SELECT id, ord, kind, text, location FROM segments WHERE path=? AND ord BETWEEN ? AND ? "
                                      "AND ord >= 0 AND kind NOT IN ('vision', 'file', 'picture') ORDER BY ord",
                                      (str(path), order - radius, order + radius)).fetchall()
        return [{'segment_id': sid, 'ord': o, 'kind': kind, 'text': text, 'loc': json.loads(loc or '{}')} for sid, o, kind, text, loc in rows]

    def vectors_for(self, segment_ids):
        """{segment_id: unit vector} for the passages that have one (for diversity among answers' sources)."""
        ids, matrix, _ = self._matrix(self._reader())
        if matrix is None or not len(ids):
            return {}
        where = {int(sid): number for number, sid in enumerate(ids)}
        return {sid: matrix[where[sid]] for sid in segment_ids if sid in where}

    # ---------------------------------------------------------------- search
    def search(self, query, limit=10, mode='hybrid', embedder=None, per_file=2, path=None, include_weak=False,
               kinds=None, modified_after=None, modified_before=None):
        """Ranked results across all tracked folders.

        Only confident matches are returned (see is_confident), so an unrelated query finds nothing. With
        ``include_weak`` the weaker matches follow them (up to ``limit`` in all), each marked 'weak': True.
        ``kinds`` (a set of KIND_SUFFIXES names) and ``modified_after`` / ``modified_before`` (timestamps, seconds)
        narrow the files considered; they apply before the per-file and ``limit`` cut, so the list still fills."""
        query = (query or '').strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        con = self._reader()
        suffixes = frozenset(s for kind in (kinds or ()) for s in KIND_SUFFIXES.get(kind, ())) if kinds else None
        after = modified_after * 1e9 if modified_after is not None else None
        before = modified_before * 1e9 if modified_before is not None else None
        filtered = suffixes is not None or after is not None or before is not None
        # The same search on an unchanged index (no commit from any connection since), with the same folders and
        # model, gives the same results: answer it from memory. Lives in _query_cache, so clearing that clears it.
        cache_key = ('results', query, limit, mode, per_file, paths.canonical(path) if path else None, bool(include_weak),
                     suffixes, after, before,
                     embedder.signature() if embedder is not None and mode != 'keyword' else None,
                     self._index_version(con), self._tracked()[0])
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return [dict(result) for result in cached]
        pool = max(limit * 4, 40) * (5 if filtered else 1)  # a filter drops candidates after ranking: look further down
        scope = self._scope_ids(con, path) if path else None
        profile = query_profile(query)
        keyword, phrases = [], []
        if mode in ('hybrid', 'keyword'):
            keyword = self._keyword(con, query, pool, scope)
            phrases = self._phrases(con, profile, pool, scope)
        semantic, note = ([], None)
        if mode in ('hybrid', 'semantic'):
            semantic, note = self._semantic(con, query, pool, embedder, scope=scope)
        ordered = self._fuse(keyword, semantic, phrases, profile.semantic_weight if mode == 'hybrid' else 1.0)
        rows = self._rows(con, [segment_id for segment_id, _ in ordered],
                          's.path, s.kind, s.text, s.location, f.size, f.mtime_ns, x.text, x.name')
        tokens = _tokens(query)
        target = paths.canonical(path) if path else None

        def result(segment_id, info, row, place, weak):
            file, kind, text, location, size, mtime = row[:6]
            loc = json.loads(location or '{}')
            found = {
                'segment_id': segment_id, 'path': file, 'filename': Path(file).name, 'folder': place[1],
                'kind': kind, 'file_type': Path(file).suffix.lower().lstrip('.'), 'location': location_label(kind, loc), **loc,
                'snippet': '' if kind == 'picture' else _snippet(text, tokens), 'score': round(info['score'], 5),
                'keyword_rank': info.get('keyword_rank'), 'semantic_rank': info.get('semantic_rank'),
                'semantic_similarity': round(info['semantic_score'], 4) if 'semantic_score' in info else None, 'size': size,
                'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime / 1e9)),
            }
            if weak:
                found['weak'] = True
            return found

        results, weak, per = [], [], {}
        for segment_id, info in ordered:
            row = rows.get(segment_id)
            if not row:
                continue
            file = row[0]
            if target and paths.canonical(file) != target:
                continue
            place = self._place(file)
            if place is None:
                continue
            if filtered and ((suffixes is not None and Path(file).suffix.lower() not in suffixes)
                             or (after is not None and row[5] < after) or (before is not None and row[5] >= before)):
                continue
            seen = '\n'.join(part for part in (row[6], row[7]) if part) or row[2]  # what keyword search saw
            if not is_confident(profile, seen, info.get('semantic_score'), embedder, kind=row[1]):
                if include_weak:
                    weak.append((segment_id, info, row, place))
                continue
            if per.get(file, 0) >= per_file:
                continue
            per[file] = per.get(file, 0) + 1
            results.append(result(segment_id, info, row, place, False))
            if len(results) >= limit:
                break
        for segment_id, info, row, place in weak:  # weaker matches after every confident one
            if len(results) >= limit:
                break
            if per.get(row[0], 0) >= per_file:
                continue
            per[row[0]] = per.get(row[0], 0) + 1
            results.append(result(segment_id, info, row, place, True))
        if note and results:
            results[0]['notes'] = [note]
        if note is None:  # a degraded search (meaning search unavailable) is not remembered
            if len(self._query_cache) > 512:
                self._query_cache.clear()
            self._query_cache[cache_key] = [dict(result) for result in results]
        return results

    @staticmethod
    def _scope_ids(con, path):
        """The passage ids of one file (matched the way paths are compared everywhere else)."""
        target = paths.canonical(path)
        indexed = [p for (p,) in con.execute('SELECT path FROM files')]
        # Indexed paths are real paths: compare them as text first, and resolve each one only if none matches.
        files = [p for p in indexed if os.path.normcase(p) == target] or [p for p in indexed if paths.canonical(p) == target]
        return [sid for file in files for (sid,) in con.execute('SELECT id FROM segments WHERE path=?', (file,))]

    @staticmethod
    def _keyword(con, query, pool, scope=None):
        tokens = _tokens(query)
        if not tokens:
            return []
        # Whole words (and simple singulars) first; then 3+ letter prefixes so results appear while typing.
        variants = list(dict.fromkeys(v for token in tokens for v in _variants(token)))
        quoted = ['"' + v.replace('"', '""') + '"' for v in variants]
        exact = ' OR '.join(quoted)
        prefix = ' OR '.join(q + '*' for q, v in zip(quoted, variants) if len(v) >= 3)
        within = ' AND rowid IN (SELECT value FROM json_each(?))' if scope is not None else ''
        sql = f'SELECT rowid, bm25(fts, 1.0, 2.5) FROM fts WHERE fts MATCH ?{within} ORDER BY bm25(fts, 1.0, 2.5) LIMIT ?'
        extra = (json.dumps(scope),) if scope is not None else ()
        try:
            ranked = [(rowid, -rank) for rowid, rank in con.execute(sql, (exact, *extra, pool))]
            if prefix and len(ranked) < pool:
                seen = {rowid for rowid, _ in ranked}
                ranked += [(rowid, -rank) for rowid, rank in con.execute(sql, (prefix, *extra, pool)) if rowid not in seen][:pool - len(ranked)]
            return ranked
        except sqlite3.OperationalError:
            return []

    def _matrix(self, con):
        """(segment ids, unit vectors as a matrix, which rows are picture vectors), rebuilt when vectors change."""
        version = (con.execute("SELECT value FROM meta WHERE key='vectors_version'").fetchone() or [None])[0]
        if self._vectors[0] != version or self._vectors[1] is None:
            ids, rows, visual = [], [], []
            for segment_id, blob, kind in con.execute('SELECT v.segment_id, v.vector, s.kind FROM vectors v '
                                                      'LEFT JOIN segments s ON s.id = v.segment_id'):
                ids.append(segment_id)
                rows.append(np.frombuffer(blob, dtype=np.float32))
                visual.append(kind == 'picture')
            dims = {r.size for r in rows}
            matrix = np.vstack(rows) if rows and len(dims) == 1 else None
            self._vectors = (version, np.asarray(ids), matrix, np.asarray(visual, dtype=bool))
        return self._vectors[1], self._vectors[2], self._vectors[3]

    def _semantic(self, con, query, pool, embedder, band=SEMANTIC_BAND, scope=None, pictures=True):
        """Meaning matches [(segment id, similarity)], best first. Text passages and picture vectors each keep the
        matches near their own best one (a picture's similarity to a text query runs on its own scale, so a strong
        picture must not push text matches out, nor the other way round). ``pictures=False``: text only."""
        if embedder is None:
            return [], None
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('embed_signature') != self.vector_signature(embedder):
            return [], 'Search by meaning is catching up — Rescan to finish.'
        ids, matrix, visual = self._matrix(con)
        if matrix is None or not len(ids):
            return [], None
        key = (embedder.signature(), query)
        if key not in self._query_cache:
            try:
                vector = np.asarray(embedder.embed_query(query), dtype=np.float32)
            except Exception as exc:
                return [], f'Search by meaning unavailable ({exc}); showing keyword matches.'
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            if len(self._query_cache) > 256:
                self._query_cache.clear()
            self._query_cache[key] = vector
        vector = self._query_cache[key]
        if vector.size != matrix.shape[1]:
            return [], 'Search by meaning needs a Rescan (the model changed).'
        scores = matrix @ vector
        if scope is not None:  # only that file's passages compete (and set the bar for "close to the best")
            scores = np.where(np.isin(ids, np.asarray(scope, dtype=ids.dtype)), scores, -np.inf)
        found = []
        for group in ((~visual, visual) if pictures and visual.any() else (~visual,)):
            part = np.where(group, scores, -np.inf)
            order = np.argsort(-part)[:pool]
            if not len(order) or not np.isfinite(part[order[0]]):
                continue
            # Keep meaning matches near the best one and above the model's relevance floor, so an unrelated
            # query returns nothing rather than the least-unrelated files.
            floor = max(getattr(embedder, 'min_similarity', 0.3), float(part[order[0]]) - band)
            found += [(int(ids[i]), float(part[i])) for i in order if part[i] >= floor]
        found.sort(key=lambda item: -item[1])
        return found, None

    # --------------------------------------------------------------- excerpts
    def read_excerpt(self, path, segment_id=None, page=None, line=None, max_chars=4000, single=False):
        """Indexed text of one file, starting at the matching passage."""
        max_chars = max(200, min(int(max_chars or 4000), 8000))
        self.resolve(path)
        key = str(path)
        con = connect(self.db_path)
        try:
            row = con.execute('SELECT status,error FROM files WHERE path=?', (key,)).fetchone()
            if not row:
                raise FileNotFoundError('This file is not indexed yet.')
            if row[0] == 'pending':
                raise ValueError('This file is still being indexed — its text is not read yet.')
            if row[0] != 'ok':
                raise ValueError(row[1] or 'This file has no searchable text')
            passages = [(sid, kind, text, json.loads(loc or '{}')) for sid, kind, text, loc in con.execute(
                'SELECT id,kind,text,location FROM segments WHERE path=? ORDER BY ord', (key,))]
        finally:
            con.close()
        vision = next(({k: loc.get(k) for k in ('caption', 'description', 'tags', 'model', 'seconds')}
                       for _, kind, _, loc in passages if kind == 'vision'), None)
        passages = [p for p in passages if p[1] not in ('vision', 'file', 'picture')]  # shown separately / just the name / no text
        start = 0
        for number, (sid, _, _, loc) in enumerate(passages):
            if (segment_id and sid == segment_id) or (page and loc.get('page') == page) or \
                    (line and loc.get('line', 0) <= line <= loc.get('end_line', 0)):
                start = number
                break
        parts, used, first, last = [], 0, None, None
        for sid, kind, text, loc in passages[start:]:
            if used >= max_chars or (single and parts):
                break
            piece = text[:max_chars - used]
            parts.append(f'[{location_label(kind, loc)}]\n{piece}' if kind in ('video', 'slide') or loc.get('ocr') else piece)
            used += len(piece)
            first = first or location_label(kind, loc)
            last = location_label(kind, loc)
        return {'path': key, 'filename': Path(key).name, 'location': first if first == last else f'{first} to {last}',
                'excerpt': '\n\n'.join(parts), 'truncated': used >= max_chars or start + len(parts) < len(passages),
                'segments_in_file': len(passages), 'vision': vision,
                'ocr_text': '\n'.join(text for _, kind, text, _ in passages if kind == 'ocr'),
                'description_pending': vision is None and self.description_pending(key)}


def _document_window(passages, order, chars=9000):
    """The document around passage ``order`` (whole, when short), for writing that passage's context."""
    texts = [text for _, text in passages]
    if sum(len(t) for t in texts) <= chars:
        return '\n\n'.join(texts)
    position = next((n for n, (o, _) in enumerate(passages) if o == order), 0)
    start = end = position
    used = len(texts[position])
    while used < chars and (start > 0 or end < len(texts) - 1):
        if start > 0:
            start -= 1
            used += len(texts[start])
        if end < len(texts) - 1 and used < chars:
            end += 1
            used += len(texts[end])
    head = texts[0] + '\n\n…\n\n' if start > 0 else ''  # the opening usually says what the document is
    return head + '\n\n'.join(texts[start:end + 1])


# ----------------------------------------------------------------- tokens
STOPWORDS = set('a an and are as at be but by for from has have how i in is it its me my of on or our that the their them this '
                'to was we were what when where which who why will with you your'.split())


def _tokens(query):
    """Query words for keyword search; common words are dropped unless the query is only common words."""
    words = [t for t in re.findall(r'\w+', query.casefold()) if t]
    meaningful = [t for t in words if t not in STOPWORDS]
    return (meaningful or words)[:16]


def _fold(text):
    """Lower-case without accents, the way the FTS tokenizer (unicode61, remove_diacritics 2) sees text."""
    text = text.casefold()
    if text.isascii():
        return text
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def _words(text):
    """The words the FTS tokenizer makes of ``text``: 'INV-2024-0042' → ['inv', '2024', '0042']."""
    return re.findall(r'[^\W_]+', _fold(text))


MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
ORDINAL_NUMBER = re.compile(r'\d+(st|nd|rd|th)')


def _date_forms(chunk):
    """Written forms of a date typed as 2024-03-15, 15/03/2024 or 03.15.2024, as word sequences: the same
    date in a document may read '2024-03-15', '15/3/2024', '15 March 2024' or 'Mar 15, 2024'."""
    match = re.fullmatch(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', chunk)
    if match:
        dates = [(int(match[1]), int(match[2]), int(match[3]))]
    else:
        match = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})', chunk)
        if not match:
            return []
        a, b, year = int(match[1]), int(match[2]), int(match[3])
        dates = [(year, b, a), (year, a, b)]  # day first or month first: both are written
    forms = []
    for year, month, day in dates:
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        y, names = str(year), (MONTHS[month - 1], MONTHS[month - 1][:3])
        for m in dict.fromkeys((f'{month:02d}', str(month))):
            for d in dict.fromkeys((f'{day:02d}', str(day))):
                forms += [(y, m, d), (d, m, y), (m, d, y)]
        for name in names:
            for d in dict.fromkeys((str(day), f'{day:02d}')):
                forms += [(d, name, y), (name, d, y)]
    return list(dict.fromkeys(forms))


def _identifier_forms(chunk):
    """Word sequences an identifier, number or date typed as one chunk may appear as in a document, or None
    when the chunk is an ordinary word. 'INV-2024-0042' → [('inv', '2024', '0042'), ('inv20240042',)]."""
    words = _words(chunk)
    if not words or all(w.isalpha() or ORDINAL_NUMBER.fullmatch(w) for w in words):
        return None  # no number in it ('2nd-year' is words, and has equivalents)
    if len(words) >= 2:  # pieces joined by - _ / . : , #
        forms = [tuple(words)] + _date_forms(chunk)
        if any(c.isalpha() for c in chunk) or re.fullmatch(r'\d{1,3}(,\d{3})+', chunk):
            forms.append((''.join(words),))  # KX-7731 written KX7731; 1,250,000 written 1250000
        return list(dict.fromkeys(forms))
    word = words[0]
    if word.isdigit():
        return [(word,)] if len(word) >= 5 else None  # long digit runs: order, account and phone numbers
    if len(word) < 4 or sum(c.isdigit() for c in word) < 2:
        return None  # 'Q3', 'mp4': too common to demand exactly
    pieces = tuple(re.findall(r'\d+|[^\W\d_]+', word))  # KX7731 written KX-7731
    return [(word,)] + ([pieces] if len(pieces) > 1 else [])


class QueryProfile:
    """How a query is matched: its identifiers (each a list of word sequences that count as an exact
    match), its meaningful words, and how much meaning search should count."""
    __slots__ = ('query', 'words', 'identifiers', 'terms', 'identifier_only', 'semantic_weight', 'partial', 'typing')

    def __init__(self, query):
        self.query = query
        self.words = _words(query)  # every word, in order (for "contains the whole query")
        chunks = [c.strip('.,;:!?()[]{}"\'“”‘’') for c in query.split()]
        chunks = [c for c in chunks if c]
        identifiers, plain, skip = [], [], False
        self.typing = None  # index of the identifier that is the query's last chunk (it may be half typed)
        for number, chunk in enumerate(chunks):
            if skip:
                skip = False
                continue
            following = chunks[number + 1] if number + 1 < len(chunks) else ''
            if (chunk.isalpha() and chunk.isupper() and 2 <= len(chunk) <= 4 and re.fullmatch(r'\d{3,}', following)):
                # 'PO 88213': a short capitalised prefix and its number, written 'PO 88213', 'PO-88213' or 'PO88213'
                prefix = _fold(chunk)
                identifiers.append([(prefix, following), (prefix + following,)] + ([(following,)] if len(following) >= 5 else []))
                if number + 2 == len(chunks):
                    self.typing = len(identifiers) - 1
                skip = True
                continue
            forms = _identifier_forms(chunk)
            if forms:
                identifiers.append(forms)
                if number + 1 == len(chunks):
                    self.typing = len(identifiers) - 1
            else:
                plain.extend(_words(chunk))
        meaningful = [w for w in plain if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]
        if not identifiers and not meaningful:
            meaningful = [w for w in plain if w not in STOPWORDS] or plain  # a query of only common words
        self.identifiers = identifiers
        # What has to be found for a keyword match to count: each identifier as a whole, and each meaningful word.
        self.terms = [('identifier', forms) for forms in identifiers] + [('word', w) for w in dict.fromkeys(meaningful)]
        self.identifier_only = bool(identifiers) and not meaningful
        self.semantic_weight = IDENTIFIER_SEMANTIC_WEIGHT if self.identifier_only else 1.0
        # The last word may still be being typed: it also counts when a word in the passage starts with it.
        last = _words(chunks[-1]) if chunks and not _identifier_forms(chunks[-1]) else []
        self.partial = last[-1] if last and len(last[-1]) >= 3 and last[-1] in meaningful else None


@functools.lru_cache(maxsize=512)
def query_profile(query):
    return QueryProfile((query or '').strip())


# ------------------------------------------------------------ relevance floor
CONFIDENT_SHARE = 0.6  # a keyword-only match must contain this share of the query's meaningful words (all of 1-2)


def semantic_floor(embedder):
    """The similarity at which a meaning match alone is worth showing, for this embedding model."""
    for name in ('confident_similarity', 'min_similarity'):
        value = getattr(embedder, name, None)
        if value is not None:
            return float(value)
    return 0.5


def _contains(words, sequence):
    """True when ``sequence`` occurs in ``words`` as adjacent words, in order."""
    size = len(sequence)
    first = sequence[0]
    return any(word == first and tuple(words[i:i + size]) == sequence for i, word in enumerate(words))


def _starts(words, sequence):
    """Like _contains, but the last word of ``sequence`` may be only the start of a word (a half-typed identifier)."""
    size = len(sequence)
    head, last = sequence[:-1], sequence[-1]
    return any(tuple(words[i:i + size - 1]) == head and len(words) > i + size - 1 and words[i + size - 1].startswith(last)
               for i in range(len(words) - size + 1))


def _has_identifier(words, forms, typing):
    return any((_starts if typing else _contains)(words, form) for form in forms)


def is_confident(query, text, semantic_similarity=None, embedder=None, kind=None):
    """Whether a passage is a strong enough match for ``query`` to show (or to answer from).

    ``text`` is what keyword search saw for the passage (its search text and file name);
    ``semantic_similarity`` its cosine similarity to the query, when meaning search scored it.
    * A picture vector (``kind`` 'picture'): its similarity must reach the model's picture floor
      (embedder.confident_picture_similarity; a model without one never makes picture matches confident).
    * A query that is only identifiers, codes or dates: the passage must contain one of them exactly.
    * The passage contains the whole query as a phrase: confident.
    * Meaning: similarity at or above the model's calibrated floor (see semantic_floor).
    * Keywords: the passage must contain most of the query's meaningful words (all of them for one or two
      words; an identifier counts as one word); a single common word matching is not enough. The last word
      may be a prefix, so results keep appearing while a word is being typed.
    """
    if kind == 'picture':
        floor = getattr(embedder, 'confident_picture_similarity', None)
        return floor is not None and semantic_similarity is not None and semantic_similarity >= floor
    profile = query if isinstance(query, QueryProfile) else query_profile(query)
    words = _words(text or '')
    if profile.identifier_only:
        return any(_has_identifier(words, forms, number == profile.typing) for number, forms in enumerate(profile.identifiers))
    if len(profile.words) >= 2 and _contains(words, tuple(profile.words)):
        return True
    if semantic_similarity is not None and semantic_similarity >= semantic_floor(embedder):
        return True
    if not profile.terms:
        return False
    vocabulary = set(words)
    found = 0
    for kind, value in profile.terms:
        if kind == 'identifier':
            found += _has_identifier(words, value, profile.typing is not None and value is profile.identifiers[profile.typing])
        else:
            found += (any(form in vocabulary for form in _variants(value)) or value + 's' in vocabulary or value + 'es' in vocabulary
                      or (value == profile.partial and any(word.startswith(value) for word in vocabulary)))
    count = len(profile.terms)
    return found >= (count if count <= 2 else math.ceil(CONFIDENT_SHARE * count))


# Documents number things in more than one way: "second semester", "Semester II", "II Sem", "2nd year".
ORDINALS = [('first', 'i', '1st'), ('second', 'ii', '2nd'), ('third', 'iii', '3rd'), ('fourth', 'iv', '4th'), ('fifth', 'v', '5th'),
            ('sixth', 'vi', '6th'), ('seventh', 'vii', '7th'), ('eighth', 'viii', '8th'), ('ninth', 'ix', '9th'), ('tenth', 'x', '10th')]
# 'i', 'v' and 'x' alone are too common to search for (or to expand).
EQUIVALENT = {form: tuple(f for f in group if len(f) > 1) for group in ORDINALS for form in group if len(form) > 1}
EQUIVALENT.update({'sem': ('sem', 'semester'), 'semester': ('semester', 'sem'), 'yr': ('yr', 'year')})


def _variants(token):
    if token in EQUIVALENT:
        return [token] + [form for form in EQUIVALENT[token] if form != token]
    forms = [token]
    if len(token) > 4 and token.endswith('ies'):
        forms.append(token[:-3] + 'y')
    elif len(token) > 3 and token.endswith('es') and token[-3] in 'sxz':
        forms.append(token[:-2])
    elif len(token) > 3 and token.endswith('s') and not token.endswith('ss'):
        forms.append(token[:-1])
    return forms


def _snippet(text, tokens, width=240):
    flat = ' '.join(text.split())
    lowered = flat.casefold()
    hits = [lowered.find(t) for t in tokens if lowered.find(t) >= 0]
    if not hits:
        return flat[:width] + ('…' if len(flat) > width else '')
    start = max(0, min(hits) - width // 3)
    piece = flat[start:start + width]
    return ('…' if start else '') + piece + ('…' if start + width < len(flat) else '')
