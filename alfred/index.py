"""The search index: every tracked folder in one SQLite database (WAL).

* Incremental: a file is re-read only when its size or modification time changes;
  deleted files and untracked folders leave the index.
* Keyword search: SQLite FTS5 with BM25 (file names weigh more than body text).
* Semantic search: EmbeddingGemma 2 vectors (normalised float32), compared with a
  dot product; vectors are cached per passage and rebuilt when the model changes.
* Hybrid: reciprocal-rank fusion of both lists. Keyword search keeps working
  when Ollama is unavailable.
"""
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

from . import extract, paths
from .extract import IMAGE_SUFFIXES, VIDEO_SUFFIXES, location_label, video_frame_png  # noqa: F401 (re-exported for the UI)

log = logging.getLogger('alfred')
RRF_K = 60
SEMANTIC_BAND = 0.06  # meaning matches must be this close to the best one (EmbeddingGemma cosine)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, root TEXT, mtime_ns INTEGER, size INTEGER, status TEXT, error TEXT,
                                 passages INTEGER, indexed_at REAL);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE TABLE IF NOT EXISTS segments(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, kind TEXT, text TEXT, location TEXT);
CREATE INDEX IF NOT EXISTS segments_path ON segments(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, name, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vectors(segment_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
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

    # ---------------------------------------------------------------- status
    def status(self):
        con = connect(self.db_path)
        try:
            counts = dict(con.execute('SELECT status, count(*) FROM files GROUP BY status'))
            meta = dict(con.execute('SELECT key,value FROM meta'))
            segments = con.execute('SELECT count(*) FROM segments').fetchone()[0]
            vectors = con.execute('SELECT count(*) FROM vectors').fetchone()[0]
        finally:
            con.close()
        roots = self.roots()
        return {'folders': len(roots), 'missing': [str(r) for r in roots if not r.is_dir()],
                'root_exists': any(r.is_dir() for r in roots), 'initialized': bool(meta.get('last_indexed')),
                'files': counts.get('ok', 0), 'errors': counts.get('error', 0), 'unsupported': counts.get('unsupported', 0),
                'segments': segments, 'vectors': vectors, 'last_indexed': meta.get('last_indexed'),
                'embed_model': meta.get('embed_signature')}

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
        return self.update(progress, embedder, force=True)

    def update(self, progress=lambda state: None, embedder=None, force=False, vision=None, roots=None):
        """Bring the index up to date with the tracked folders (or just ``roots``). Returns a summary."""
        if not self._lock.acquire(blocking=False):
            raise IndexBusy('Indexing is already running')
        self.cancel.clear()
        try:
            con = connect(self.db_path)
            try:
                return self._update(con, progress, embedder, force, roots)
            finally:
                con.close()
        finally:
            self._lock.release()

    def _update(self, con, progress, embedder, force, only):
        started = time.time()
        summary = {'found': 0, 'changed': 0, 'unchanged': 0, 'removed': 0, 'indexed': 0, 'errors': 0, 'unsupported': 0,
                   'embedded': 0, 'warnings': []}
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
            if reread_media:
                for p in list(known):
                    suffix = Path(p).suffix.lower()
                    if suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES or suffix == '.pdf':
                        known[p] = None
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
        if embedder is not None and not self.cancel.is_set():
            summary['embedded'] = self._embed_missing(con, embedder, progress, summary)
        with con:
            con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('last_indexed', time.strftime('%Y-%m-%d %H:%M')))
            if not self.cancel.is_set():
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('media_signature', signature))
        summary['seconds'] = round(time.time() - started, 2)
        progress({'phase': 'done', 'done': 1, 'total': 1})
        return summary

    def _index_file(self, con, root, path, st, ocr_on, interval, frame_progress, summary):
        status, error, passages = 'ok', '', []
        try:
            passages = extract.extract(path, ocr_on, interval, frame_progress, self.cancel)
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
            self._delete_passages(con, [path])
            name = name_terms(path)
            for order, (kind, text, loc) in enumerate(passages):
                cursor = con.execute('INSERT INTO segments(path,ord,kind,text,location) VALUES(?,?,?,?,?)',
                                     (path, order, kind, text, json.dumps(loc)))
                con.execute('INSERT INTO fts(rowid,text,name) VALUES(?,?,?)', (cursor.lastrowid, text, name))
            con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)',
                        (path, str(root), st.st_mtime_ns, st.st_size, status, error, len(passages), time.time()))
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
        con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vectors_version', str(time.time())))

    def _embed_missing(self, con, embedder, progress, summary):
        """Embed passages that have no vector yet, in batches; re-embed everything after a model change."""
        signature = embedder.signature()
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('embed_signature') != signature:
            with con:
                con.execute('DELETE FROM vectors')
                con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('embed_signature', signature))
        rows = list(con.execute('SELECT s.id, s.path, s.text FROM segments s LEFT JOIN vectors v ON v.segment_id=s.id '
                                'WHERE v.segment_id IS NULL'))
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
            keyword = self._keyword(con, query, pool) if mode in ('hybrid', 'keyword') else []
            semantic, note = ([], None)
            if mode in ('hybrid', 'semantic'):
                semantic, note = self._semantic(con, query, pool, embedder)
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
    def _keyword(con, query, pool):
        tokens = _tokens(query)
        if not tokens:
            return []
        # Whole words (and simple singulars) first; then 3+ letter prefixes so results appear while typing.
        variants = list(dict.fromkeys(v for token in tokens for v in _variants(token)))
        quoted = ['"' + v.replace('"', '""') + '"' for v in variants]
        exact = ' OR '.join(quoted)
        prefix = ' OR '.join(q + '*' for q, v in zip(quoted, variants) if len(v) >= 3)
        sql = 'SELECT rowid, bm25(fts, 1.0, 2.5) FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 1.0, 2.5) LIMIT ?'
        try:
            ranked = [(rowid, -rank) for rowid, rank in con.execute(sql, (exact, pool))]
            if prefix and len(ranked) < pool:
                seen = {rowid for rowid, _ in ranked}
                ranked += [(rowid, -rank) for rowid, rank in con.execute(sql, (prefix, pool)) if rowid not in seen][:pool - len(ranked)]
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

    def _semantic(self, con, query, pool, embedder):
        if embedder is None:
            return [], None
        meta = dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('embed_signature') != embedder.signature():
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
        order = np.argsort(-scores)[:pool]
        # Keep meaning matches near the best one and above the model's relevance floor, so an unrelated
        # query returns nothing rather than the least-unrelated files.
        floor = max(getattr(embedder, 'min_similarity', 0.3), float(scores[order[0]]) - SEMANTIC_BAND)
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
                'segments_in_file': len(passages)}


# ----------------------------------------------------------------- tokens
def _tokens(query):
    return [t for t in re.findall(r'\w+', query.casefold()) if t][:16]


def _variants(token):
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
