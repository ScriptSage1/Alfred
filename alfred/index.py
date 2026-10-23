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
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat as file_stat
import threading
import time
from pathlib import Path

import numpy as np

from . import ai, extract, paths
from .extract import IMAGE_SUFFIXES, VIDEO_SUFFIXES, location_label, video_frame_png  # noqa: F401 (re-exported for the UI)

log = logging.getLogger('alfred')
RRF_K = 60
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
'''


class IndexBusy(RuntimeError):
    pass


def connect(db_path):
    con = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    return con


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
        self._vectors = (None, None, None)  # (version, ids, matrix)
        self._query_cache = {}
        connect(self.db_path).close()

    # ---------------------------------------------------------------- folders
    def roots(self):
        return [Path(os.path.realpath(folder)) for folder in self.settings.folders]

    def root_of(self, path):
        return next((root for root in self.roots() if paths.is_within(root, path)), None)

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
        root = self.root_of(path)
        if root is None:
            return str(Path(path).parent)
        rel = Path(path).parent.relative_to(root) if paths.is_within(root, Path(path).parent) else Path()
        return ' › '.join([root.name] + list(rel.parts))

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
                'files': counts.get('ok', 0), 'errors': counts.get('error', 0), 'unsupported': counts.get('unsupported', 0),
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
        """{root: {'files': searchable, 'skipped': unsupported or failed}} for each tracked folder."""
        con = connect(self.db_path)
        try:
            rows = list(con.execute('SELECT root, status, count(*) FROM files GROUP BY root, status'))
        finally:
            con.close()
        stats = {str(root): {'files': 0, 'skipped': 0} for root in self.roots()}
        for root, status, count in rows:
            if root in stats:
                stats[root]['files' if status == 'ok' else 'skipped'] += count
        return stats

    def problems(self, limit=50):
        con = connect(self.db_path)
        try:
            return [{'path': p, 'status': s, 'error': e} for p, s, e in con.execute(
                "SELECT path,status,error FROM files WHERE status='error' ORDER BY path LIMIT ?", (limit,))]
        finally:
            con.close()

    def pending_changes(self, cap=20000):
        """Files on disk the index hasn't caught up with (bounded, read-only)."""
        con = connect(self.db_path)
        try:
            known = {p: (m, s) for p, m, s in con.execute('SELECT path,mtime_ns,size FROM files')}
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

    def update(self, progress=lambda state: None, embedder=None, force=False, vision=None, roots=None):
        """Bring the index up to date with the tracked folders (or just ``roots``). Returns a summary."""
        if not self._lock.acquire(blocking=False):
            raise IndexBusy('Indexing is already running')
        self.cancel.clear()
        try:
            con = connect(self.db_path)
            try:
                return self._update(con, progress, embedder, force, roots, vision)
            finally:
                con.close()
        finally:
            self._lock.release()

    def _update(self, con, progress, embedder, force, only, vision=None):
        started = time.time()
        summary = {'found': 0, 'changed': 0, 'unchanged': 0, 'removed': 0, 'indexed': 0, 'errors': 0, 'unsupported': 0,
                   'embedded': 0, 'described': 0, 'warnings': []}
        tracked = self.roots()
        tracked_keys = {str(r) for r in tracked}
        # Files of folders that are no longer tracked leave the index.
        stale = [p for p, root in con.execute('SELECT path, root FROM files') if root not in tracked_keys]
        if force:
            stale = [p for (p,) in con.execute('SELECT path FROM files')]
        self._delete_files(con, stale)
        summary['removed'] += len(stale) if not force else 0
        ocr_on = bool(self.settings.get('ocr_enabled', True))
        interval = max(1, int(self.settings.get('video_interval') or 10))
        signature = f'ocr:{ocr_on}|video:{interval}'
        meta = dict(con.execute('SELECT key,value FROM meta'))
        reread_media = meta.get('media_signature') != signature
        search_signature = self.search_signature()
        reread_documents = (meta.get('search_signature') or '').split('|')[0] != ENRICH_VERSION
        roots = [r for r in tracked if only is None or any(paths.canonical(r) == paths.canonical(o) for o in only)]
        for root in roots:
            if self.cancel.is_set():
                break
            if not root.is_dir():
                summary['warnings'].append(f'{root.name} is missing')
                continue
            progress({'phase': 'scanning', 'done': 0, 'total': 0, 'root': str(root)})
            found = list(discover(root))
            known = {p: (m, s) for p, m, s in con.execute('SELECT path,mtime_ns,size FROM files WHERE root=?', (str(root),))}
            for p in list(known):
                suffix = Path(p).suffix.lower()
                if reread_media and (suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES or suffix == '.pdf'):
                    known[p] = None
                if reread_documents and suffix in REREAD_FOR_ENRICH:
                    known[p] = None  # read again for the headings and titles the chunk headers use
            present = {p for p, _ in found}
            removed = [p for p in known if p not in present]
            self._delete_files(con, removed)
            todo = [(p, st) for p, st in found if known.get(p) != (st.st_mtime_ns, st.st_size)]
            summary['found'] += len(found)
            summary['changed'] += len(todo)
            summary['unchanged'] += len(found) - len(todo)
            summary['removed'] += len(removed)
            for number, (path, st) in enumerate(todo):
                if self.cancel.is_set():
                    summary['warnings'].append('Stopped early; finished files were kept')
                    break
                if not any(paths.canonical(r) == paths.canonical(root) for r in self.roots()):
                    break  # untracked while indexing: remove_root cleans up what was written
                state = {'phase': 'indexing', 'done': number, 'total': len(todo), 'current': path, 'root': str(root)}
                progress(state)
                frame_progress = lambda done, total, state=state: progress(dict(state, phase='reading video', frame=done, frames=total))
                self._index_file(con, root, path, st, ocr_on, interval, frame_progress, summary)
            progress({'phase': 'indexing', 'done': len(todo), 'total': len(todo), 'root': str(root)})
        if meta.get('search_signature') != search_signature and not self.cancel.is_set():
            self._refresh_search_text(con)
        if embedder is not None and not self.cancel.is_set():
            summary['embedded'] = self._embed_missing(con, embedder, progress, summary)
        # PDFs are searchable by now (their text was read quickly); their tables are read row by row last.
        if not self.cancel.is_set() and self._read_pdf_tables(con, ocr_on, progress, summary) and embedder is not None:
            summary['embedded'] += self._embed_missing(con, embedder, progress, summary)
        if vision is not None and not self.cancel.is_set():
            # Slowest steps last, so files are searchable (by text and meaning) before pictures are described.
            self._describe_missing(con, vision, embedder, progress, summary)
        writer = ai.Contextualizer.from_settings(self.settings)
        if writer is not None and not self.cancel.is_set():
            self._contextualize_missing(con, writer, embedder, progress, summary)
        with con:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_indexed', time.strftime('%Y-%m-%d %H:%M')))
            if not self.cancel.is_set():
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('media_signature', signature))
                if only is None or not reread_documents:  # documents of every folder were read with the new headers
                    con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('search_signature', search_signature))
        summary['seconds'] = round(time.time() - started, 2)
        progress({'phase': 'done', 'done': 1, 'total': 1})
        return summary

    def _read_pdf_tables(self, con, ocr_on, progress, summary):
        """Read the tables of PDFs indexed with the quick reader (or an older table reader). True if any changed."""
        tracked = {str(r) for r in self.roots()}
        todo = [(path, root) for path, root in con.execute(
            "SELECT f.path, f.root FROM files f LEFT JOIN pdf_tables t ON t.path = f.path WHERE f.status='ok' "
            "AND lower(f.path) LIKE '%.pdf' AND (t.reader IS NULL OR t.reader != ?)", (PDF_READER,)) if root in tracked]
        changed = False
        for number, (path, root) in enumerate(todo):
            if self.cancel.is_set():
                break
            progress({'phase': 'reading tables', 'done': number, 'total': len(todo), 'current': path, 'root': root})
            try:
                import pymupdf
                with pymupdf.open(path) as document:
                    has_tables = bool(extract.table_pages(document))
                st = os.stat(path)
            except Exception as error:  # gone, locked or unreadable now: left for the next update to try again
                log.warning('Could not read the tables of %s: %s', path, error)
                continue
            if has_tables:
                before = {k: summary[k] for k in ('indexed', 'errors', 'unsupported')}
                self._index_file(con, Path(root), path, st, ocr_on, 10, None, summary, pdf_tables=True)
                summary.update(before)  # the same file again: not counted twice
                changed = True
            else:
                with con:
                    con.execute('INSERT OR REPLACE INTO pdf_tables VALUES(?,?)', (path, PDF_READER))
        return changed

    def _index_file(self, con, root, path, st, ocr_on, interval, frame_progress, summary, pdf_tables=False):
        status, error, passages = 'ok', '', []
        try:
            passages = extract.extract(path, ocr_on, interval, frame_progress, self.cancel, pdf_tables=pdf_tables)
            if not passages and Path(path).suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
                # A photo or video without text is normal: keep it findable by its name.
                passages = [('file', Path(path).stem, {'name_only': True})]
            elif not passages:
                status, error = 'error', 'No readable text'
        except extract.Unsupported as reason:
            status, error = 'unsupported', str(reason)
        except Exception as exc:  # a bad file never stops the run
            status, error = 'error', (str(exc) or type(exc).__name__)[:300]
            log.warning('Could not index %s: %s', path, error)
        with con:
            # Passages that read the same as before keep their vectors: an edited file (or one read again by a
            # newer reader) only re-embeds what changed.
            kept = {text: (dim, vector) for text, dim, vector in con.execute(
                'SELECT f.text, v.dim, v.vector FROM segments s JOIN fts f ON f.rowid = s.id '
                'JOIN vectors v ON v.segment_id = s.id WHERE s.path=?', (path,))}
            self._delete_passages(con, [path])
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
            if pdf_tables:
                con.execute('INSERT OR REPLACE INTO pdf_tables VALUES(?,?)', (path, PDF_READER))
            else:
                con.execute('DELETE FROM pdf_tables WHERE path=?', (path,))
        summary['indexed' if status == 'ok' else 'errors' if status == 'error' else 'unsupported'] += 1

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
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

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
            if self.cancel.is_set():
                break
            progress({'phase': 'describing', 'done': number, 'total': len(rows), 'current': path, 'root': root})
            began = time.time()
            try:
                image, seconds = self._picture(path)
                described = vision.describe(image)
            except ai.ProviderError as error:
                if 'Cannot reach' in str(error) or 'not installed' in str(error):
                    summary['warnings'].append(f'Picture descriptions paused: {error}')
                    break  # Ollama or the model is unavailable: try again on the next run
                self._record_description(con, path, signature, 'error', str(error)[:300], time.time() - began)
                continue
            except Exception as error:  # an unreadable picture never stops the run
                self._record_description(con, path, signature, 'error', (str(error) or type(error).__name__)[:300], time.time() - began)
                continue
            location = dict(ai=True, model=vision.model, **described)
            if seconds is not None:
                location['seconds'] = seconds
            text = '\n'.join(part for part in (described['caption'], described['description'],
                                                'Tags: ' + ', '.join(described['tags']) if described['tags'] else '') if part)
            with con:
                for (old,) in list(con.execute("SELECT id FROM segments WHERE path=? AND kind='vision'", (path,))):
                    con.execute('DELETE FROM fts WHERE rowid=?', (old,))
                    con.execute('DELETE FROM vectors WHERE segment_id=?', (old,))
                    con.execute('DELETE FROM segments WHERE id=?', (old,))
                cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)',
                                     (path, -1, 'vision', text, json.dumps(location)))
                con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)',
                            (cursor.lastrowid, self._search_text(con, path, 'vision', text, location, None), name_terms(path)))
            self._record_description(con, path, signature, 'ok', '', time.time() - began)
            summary['described'] += 1
            pending_embeds += 1
            if embedder is not None and pending_embeds >= DESCRIBE_EMBED_EVERY:
                summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)
                pending_embeds = 0
        if embedder is not None and pending_embeds:
            summary['embedded'] += self._embed_missing(con, embedder, lambda state: None, summary)

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

    def _embed_missing(self, con, embedder, progress, summary):
        """Embed passages that have no vector yet, in batches; re-embed everything after a model change."""
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
            if self.cancel.is_set():
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
            if self.cancel.is_set():
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
        con = connect(self.db_path)
        try:
            scope = self._scope_ids(con, path) if path else None  # a one-file chat searches only that file
            keyword = self._keyword(con, query, pool, scope)
            semantic, _ = self._semantic(con, query, pool, embedder, band, scope)
            fused = {}
            for channel, ranked in (('keyword', keyword), ('semantic', semantic)):
                for rank, (segment_id, raw) in enumerate(ranked):
                    item = fused.setdefault(segment_id, {'score': 0.0})
                    item['score'] += 1.0 / (RRF_K + rank + 1)
                    item[channel + '_rank'] = rank + 1
                    item[channel + '_score'] = float(raw)
            roots = self.roots()
            found = []
            for segment_id, info in sorted(fused.items(), key=lambda kv: -kv[1]['score']):
                row = con.execute("SELECT s.path, s.ord, s.kind, s.text, s.location FROM segments s JOIN files f ON f.path = s.path "
                                  "WHERE s.id=? AND f.status='ok'", (segment_id,)).fetchone()
                if not row or row[2] == 'file':
                    continue
                file, order, kind, text, location = row
                if path and paths.canonical(file) != paths.canonical(path):
                    continue
                if not any(paths.is_within(root, file) for root in roots):
                    continue
                loc = json.loads(location or '{}')
                found.append(dict(info, segment_id=segment_id, path=file, ord=order, kind=kind, text=text, loc=loc,
                                  header=self.header(file, kind, loc)))
                if len(found) >= keep:
                    break
            return found
        finally:
            con.close()

    def neighbours(self, path, order, radius=1):
        """Passages next to passage ``order`` of ``path`` (same file, readable text only), in order."""
        con = connect(self.db_path)
        try:
            rows = con.execute("SELECT id, ord, kind, text, location FROM segments WHERE path=? AND ord BETWEEN ? AND ? "
                               "AND kind NOT IN ('vision', 'file') ORDER BY ord", (str(path), order - radius, order + radius)).fetchall()
        finally:
            con.close()
        return [{'segment_id': sid, 'ord': o, 'kind': kind, 'text': text, 'loc': json.loads(loc or '{}')} for sid, o, kind, text, loc in rows]

    def vectors_for(self, segment_ids):
        """{segment_id: unit vector} for the passages that have one (for diversity among answers' sources)."""
        con = connect(self.db_path)
        try:
            ids, matrix = self._matrix(con)
        finally:
            con.close()
        if matrix is None or not len(ids):
            return {}
        where = {int(sid): number for number, sid in enumerate(ids)}
        return {sid: matrix[where[sid]] for sid in segment_ids if sid in where}

    # ---------------------------------------------------------------- search
    def search(self, query, limit=10, mode='hybrid', embedder=None, per_file=2, path=None):
        """Ranked results across all tracked folders."""
        query = (query or '').strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        con = connect(self.db_path)
        try:
            pool = max(limit * 4, 40)
            scope = self._scope_ids(con, path) if path else None
            keyword = self._keyword(con, query, pool, scope) if mode in ('hybrid', 'keyword') else []
            semantic, note = ([], None)
            if mode in ('hybrid', 'semantic'):
                semantic, note = self._semantic(con, query, pool, embedder, scope=scope)
            fused = {}
            for channel, ranked in (('keyword', keyword), ('semantic', semantic)):
                for rank, (segment_id, raw) in enumerate(ranked):
                    item = fused.setdefault(segment_id, {'score': 0.0})
                    item['score'] += 1.0 / (RRF_K + rank + 1)
                    item[channel + '_rank'] = rank + 1
                    item[channel + '_score'] = round(float(raw), 4)
            ordered = sorted(fused.items(), key=lambda kv: -kv[1]['score'])
            results, per = [], {}
            tokens = _tokens(query)
            roots = self.roots()
            for segment_id, info in ordered:
                row = con.execute('SELECT s.path, s.kind, s.text, s.location, f.size, f.mtime_ns FROM segments s '
                                  'JOIN files f ON f.path = s.path WHERE s.id=?', (segment_id,)).fetchone()
                if not row:
                    continue
                file, kind, text, location, size, mtime = row
                if path and paths.canonical(file) != paths.canonical(path):
                    continue
                if not any(paths.is_within(root, file) for root in roots):
                    continue
                if per.get(file, 0) >= per_file:
                    continue
                per[file] = per.get(file, 0) + 1
                loc = json.loads(location or '{}')
                results.append({
                    'segment_id': segment_id, 'path': file, 'filename': Path(file).name, 'folder': self.display_folder(file),
                    'kind': kind, 'file_type': Path(file).suffix.lower().lstrip('.'), 'location': location_label(kind, loc), **loc,
                    'snippet': _snippet(text, tokens), 'score': round(info['score'], 5),
                    'keyword_rank': info.get('keyword_rank'), 'semantic_rank': info.get('semantic_rank'),
                    'semantic_similarity': info.get('semantic_score'), 'size': size,
                    'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime / 1e9)),
                })
                if len(results) >= limit:
                    break
            if note and results:
                results[0]['notes'] = [note]
            return results
        finally:
            con.close()

    @staticmethod
    def _scope_ids(con, path):
        """The passage ids of one file (matched the way paths are compared everywhere else)."""
        target = paths.canonical(path)
        files = [p for (p,) in con.execute('SELECT path FROM files') if paths.canonical(p) == target]
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
        version = (con.execute("SELECT value FROM meta WHERE key='vectors_version'").fetchone() or [None])[0]
        if self._vectors[0] != version or self._vectors[1] is None:
            ids, rows = [], []
            for segment_id, dim, blob in con.execute('SELECT segment_id, dim, vector FROM vectors'):
                ids.append(segment_id)
                rows.append(np.frombuffer(blob, dtype=np.float32))
            dims = {r.size for r in rows}
            matrix = np.vstack(rows) if rows and len(dims) == 1 else None
            self._vectors = (version, np.asarray(ids), matrix)
        return self._vectors[1], self._vectors[2]

    def _semantic(self, con, query, pool, embedder, band=SEMANTIC_BAND, scope=None):
        if embedder is None:
            return [], None
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('embed_signature') != self.vector_signature(embedder):
            return [], 'Search by meaning is catching up — Rescan to finish.'
        ids, matrix = self._matrix(con)
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
        order = np.argsort(-scores)[:pool]
        if not np.isfinite(scores[order[0]]):
            return [], None
        # Keep meaning matches near the best one and above the model's relevance floor, so an unrelated
        # query returns nothing rather than the least-unrelated files.
        floor = max(getattr(embedder, 'min_similarity', 0.3), float(scores[order[0]]) - band)
        return [(int(ids[i]), float(scores[i])) for i in order if scores[i] >= floor], None

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
            if row[0] != 'ok':
                raise ValueError(row[1] or 'This file has no searchable text')
            passages = [(sid, kind, text, json.loads(loc or '{}')) for sid, kind, text, loc in con.execute(
                'SELECT id,kind,text,location FROM segments WHERE path=? ORDER BY ord', (key,))]
        finally:
            con.close()
        vision = next(({k: loc.get(k) for k in ('caption', 'description', 'tags', 'model', 'seconds')}
                       for _, kind, _, loc in passages if kind == 'vision'), None)
        passages = [p for p in passages if p[1] not in ('vision', 'file')]  # shown separately / just the name
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
