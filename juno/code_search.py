"""Code search: find code in the tracked folders by what it does, by name, and by who calls it.

For AI tools that use Juno's MCP server (the search_code tool). The app builds this index during its normal update
(Index.update, after the meaning layer); the MCP server only reads it.

* Chunks: Python is split with ast (the module header, each function and method, each class header with its member
  list; functions longer than MAX_FUNCTION_LINES in overlapping windows). Other languages: WINDOW-line windows.
* Keyword: SQLite FTS5 with BM25. Identifiers are split (``_embed_missing`` -> embed, missing; ``IndexBusy`` ->
  index, busy) and symbol and file names weigh more than body text.
* Meaning: the same EmbeddingGemma vectors as file search (juno.ai.Embedder), so a behaviour described in plain
  words finds the function that does it. Without an embedder the index is keyword only.
* Ranking: reciprocal-rank fusion of the two lists, plus a boost when the query names a symbol exactly.
* Structure: each result lists its callers (at the line of the call) and callees (at their definition) from a Python
  call graph built from the chunks (no dependency; precision first: an unknown call target gives no edge, see
  CallGraph.build).

Measured (benchmarker/code_search/REPORT.md, 2026-11-16): with this tool Claude Code needed 24-32% fewer tool calls on
two codebases (25k and 103k lines) and 14% fewer tokens on the larger one, with the same answers.

Storage: code.sqlite in the Juno data folder, separate from index.sqlite. Paths are absolute, as in index.sqlite.
"""
import ast
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SCHEMA = 'code-v1'
CODE_SUFFIXES = frozenset({
    '.py', '.pyw', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.go', '.rs', '.java', '.kt', '.kts', '.c', '.h', '.cc',
    '.cpp', '.cxx', '.hpp', '.cs', '.rb', '.php', '.swift', '.scala', '.lua', '.sh', '.bash', '.ps1', '.sql', '.r',
    '.dart', '.vue', '.svelte', '.zig', '.m', '.mm', '.pl', '.ex', '.exs', '.erl', '.hs', '.ml', '.fs', '.jl', '.cmd', '.bat',
})
# Folders of installed or generated code: never the user's own (paths.skip_dir already drops dot-folders and the like).
SKIP_PARTS = frozenset({'node_modules', '__pycache__', '.venv', 'venv', 'site-packages', 'dist-packages', 'graphify-out',
                        '.tox', '.mypy_cache', '.pytest_cache', 'bower_components', 'vendor', '.next', '.nuxt'})
MAX_FILE_BYTES = 2 * 2 ** 20
MAX_FUNCTION_LINES = 80
WINDOW, STEP = 60, 50
SNIPPET_LINES, SNIPPET_CHARS = 14, 900
RRF_K = 60                 # tried 5-60 on the benchmark: differences within one question
CANDIDATES = 60
EXACT_BOOST = 1.0          # beats any fused score (at most 2 / 61)
NEIGHBOURS = 6
EMBED_BATCH = 32
STOP = frozenset({'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'is', 'are', 'for', 'on', 'with', 'what', 'where', 'which',
                  'how', 'does', 'do', 'it', 'that', 'this', 'by', 'from', 'when', 'who', 'be', 'as', 'at', 'its', 'into', 'there',
                  'code', 'function', 'functions', 'method', 'file', 'files', 'find', 'show', 'me', 'i', 'we', 'called', 'call',
                  'calls'})

IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
CAMEL = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+')
WORD = re.compile(r'[A-Za-z0-9]+')

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, root TEXT, sig TEXT);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, start INTEGER, end INTEGER, symbol TEXT, kind TEXT,
                                  text TEXT, def_line INTEGER, calls TEXT);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, names, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vectors(id INTEGER PRIMARY KEY, vector BLOB);
"""


class CodeIndexMissing(RuntimeError):
    """There is no code index (yet), or it was made by another version."""


# ------------------------------------------------------------------ words
def split_identifier(name):
    """'_embed_missing' -> ['embed', 'missing']; 'IndexBusy' -> ['index', 'busy']; 'HTTPServer2' -> ['http', 'server', '2']."""
    parts = []
    for piece in name.split('_'):
        parts.extend(p.lower() for p in CAMEL.findall(piece))
    return [p for p in parts if p]


def code_words(text):
    """Words FTS should also see: the camelCase parts of identifiers (snake_case already splits on '_')."""
    extra = []
    for ident in set(IDENT.findall(text)):
        if any(c.isupper() for c in ident[1:]) and any(c.islower() for c in ident):
            extra.extend(split_identifier(ident))
    return ' '.join(extra)


def query_terms(query):
    """Search terms of a question: identifier parts, lower case, no stop words, in order, once each."""
    terms = []
    for token in WORD.findall(query.replace('_', ' ')):
        for part in split_identifier(token) or [token.lower()]:
            if part not in STOP and part not in terms and (len(part) > 1 or part.isdigit()):
                terms.append(part)
    return terms


def named_symbols(query):
    """Identifiers the query names outright: '_embed_missing', 'IndexBusy', 'search()', 'Index.search'."""
    names = set()
    for token in re.findall(r'[A-Za-z_][A-Za-z0-9_.]*', query):
        token = token.strip('.')
        last = token.rsplit('.', 1)[-1]
        if not last:
            continue
        looks_code = '_' in last or '.' in token or (any(c.isupper() for c in last[1:]) and any(c.islower() for c in last))
        if looks_code or f'{token}(' in query:
            names.add(last)
    return names


def is_code(path):
    """Whether ``path`` is a code file this index reads (by suffix, outside installed-package folders)."""
    p = Path(path)
    return p.suffix.lower() in CODE_SUFFIXES and not any(part.lower() in SKIP_PARTS for part in p.parts[:-1])


# ------------------------------------------------------------------ chunks
@dataclass
class Chunk:
    path: str
    start: int           # first line, 1-based
    end: int             # last line, inclusive
    symbol: str          # 'Index.search'; '' for a block
    kind: str            # function | class | module | block
    text: str
    def_line: int = 0    # the def/class line; 0 for a block
    calls: list = field(default_factory=list)  # 'name' or 'receiver.name' (Python only)
    call_lines: list = field(default_factory=list)  # the line of each call's first use, in step with calls

    @property
    def name(self):
        return self.symbol.rsplit('.', 1)[-1]


def _windows(path, lines, first, last, symbol='', kind='block', header=None, def_line=0):
    out, start = [], first
    while start <= last:
        end = min(last, start + WINDOW - 1)
        body = '\n'.join(lines[start - 1:end])
        if header and start != first:
            body = header + '\n    ...\n' + body
        if body.strip():
            out.append(Chunk(path, start, end, symbol, kind, body, def_line))
        if end == last:
            break
        start += STEP
    return out


class _Calls(ast.NodeVisitor):
    """'name' for f(), 'receiver.name' for x.f() (receiver = the last name before the dot: self.index.search() ->
    'index.search'; '?' when it is not a name, as in f().g())."""

    def __init__(self):
        self.names = []
        self.lines = []

    def visit_Call(self, node):
        f = node.func
        name = None
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            value = f.value
            receiver = value.id if isinstance(value, ast.Name) else value.attr if isinstance(value, ast.Attribute) else '?'
            name = f'{receiver}.{f.attr}'
        if name and name not in self.names:
            self.names.append(name)
            self.lines.append(node.lineno)
        self.generic_visit(node)


def _called(node):
    """(names, lines) of the calls in a function body."""
    visitor = _Calls()
    for child in node.body:
        visitor.visit(child)
    return visitor.names, visitor.lines


def python_chunks(path, source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return window_chunks(path, source)
    lines = source.splitlines()
    out, covered = [], []

    def start_of(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol = prefix + node.name
                first, last = start_of(node), node.end_lineno
                covered.append((first, last))
                calls, call_lines = _called(node)
                if last - first + 1 > MAX_FUNCTION_LINES:
                    parts = _windows(path, lines, first, last, symbol, 'function', lines[node.lineno - 1].rstrip(), node.lineno)
                    for part in parts:
                        part.calls, part.call_lines = calls, call_lines
                    out.extend(parts)
                else:
                    out.append(Chunk(path, first, last, symbol, 'function', '\n'.join(lines[first - 1:last]), node.lineno, calls,
                                     call_lines))
            elif isinstance(node, ast.ClassDef):
                symbol = prefix + node.name
                first, last = start_of(node), node.end_lineno
                covered.append((first, last))
                members = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                head_end = min((start_of(members[0]) - 1) if members else last, first + MAX_FUNCTION_LINES - 1)
                text = '\n'.join(lines[first - 1:head_end]).rstrip()
                if members:
                    text += '\n    # members: ' + ', '.join(m.name for m in members)
                out.append(Chunk(path, first, head_end, symbol, 'class', text, node.lineno))
                visit(node.body, symbol + '.')

    visit(tree.body, '')
    # Top-level code outside every def and class (docstring, imports, constants, script code): windows.
    taken = [False] * (len(lines) + 2)
    for first, last in covered:
        for i in range(first, min(last, len(lines)) + 1):
            taken[i] = True
    first_text = next((k for k in range(1, len(lines) + 1) if lines[k - 1].strip()), 1)
    i, n = 1, len(lines)
    while i <= n:
        if taken[i] or not lines[i - 1].strip():
            i += 1
            continue
        j = i
        while j + 1 <= n and not taken[j + 1]:
            j += 1
        for chunk in _windows(path, lines, i, j):
            chunk.kind = 'module' if chunk.start == first_text else 'block'
            out.append(chunk)
        i = j + 1
    out.sort(key=lambda c: (c.start, c.end))
    return out


def window_chunks(path, source):
    lines = source.splitlines()
    return _windows(path, lines, 1, len(lines)) if lines else []


def chunks_of(path, source):
    return python_chunks(path, source) if str(path).lower().endswith(('.py', '.pyw')) else window_chunks(path, source)


def read_source(path):
    with open(path, 'rb') as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or b'\0' in data[:4096]:
        return None
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


# ------------------------------------------------------------------ call graph
class CallGraph:
    """Callers and callees by (path, def line)."""

    def __init__(self):
        # (path, def line) -> callers [(path, line of the call, symbol)] / callees [(path, def line, symbol)]
        self.callers, self.callees = {}, {}

    def _add(self, a, b, at=0):
        """a (path, def line, symbol) calls b at line ``at``."""
        out, into = self.callees.setdefault(a[:2], []), self.callers.setdefault(b[:2], [])
        site = (a[0], at or a[1], a[2])
        if b not in out:
            out.append(b)
        if site not in into:
            into.append(site)

    def neighbours(self, path, line):
        return self.callers.get((path, line), []), self.callees.get((path, line), [])

    @classmethod
    def build(cls, chunks):
        """Precision first: a call whose target is unknown gives no edge.
        * f()         the definition of f in the same file, else the only one anywhere.
        * self.f()    method f of the caller's own class, else the only f in the same file.
        * mod.f()     top-level f of the file named mod.py.
        * thing.f()   method f of the class whose name matches the receiver ('index' -> Index,
                      'folder_watcher' -> FolderWatcher): Python's naming habit stands in for types.
        On Juno's own code this found all four real callers of Index.search; Graphify's graph found none of them."""
        defs, by_symbol, classes, modules = {}, {}, {}, {}
        for c in chunks:
            if c.kind in ('function', 'class') and c.def_line:
                key = (c.path, c.def_line)
                defs.setdefault(c.name, {})[key] = c.symbol
                by_symbol.setdefault(c.symbol, {})[key] = c.symbol
                if c.kind == 'class':
                    classes.setdefault(c.name.lower().lstrip('_'), []).append(c.symbol)
            modules.setdefault(Path(c.path).stem.lower(), set()).add(c.path)

        def resolve(caller, call):
            receiver, _, name = call.rpartition('.')
            if not receiver:
                options = defs.get(name) or {}
                local = {k: v for k, v in options.items() if k[0] == caller.path}
                return local if len(local) == 1 else options if len(options) == 1 else {}
            if receiver in ('self', 'cls'):
                if '.' in caller.symbol:
                    own = {k: v for k, v in (by_symbol.get(caller.symbol.rsplit('.', 1)[0] + '.' + name) or {}).items()
                           if k[0] == caller.path}
                    if own:
                        return own
                local = {k: v for k, v in (defs.get(name) or {}).items() if k[0] == caller.path}
                return local if len(local) == 1 else {}
            if receiver.lower() in modules:
                found = {k: v for k, v in (by_symbol.get(name) or {}).items() if k[0] in modules[receiver.lower()]}
                if len(found) == 1:
                    return found
            found = {}
            for owner in classes.get(receiver.lower().lstrip('_').replace('_', '')) or []:
                found.update(by_symbol.get(f'{owner}.{name}') or {})
            return found if len(found) == 1 else {}

        graph, seen = cls(), set()
        for c in chunks:
            if not c.calls or (c.path, c.def_line) in seen:
                continue
            seen.add((c.path, c.def_line))
            lines = c.call_lines if len(c.call_lines) == len(c.calls) else [0] * len(c.calls)
            for call, at in zip(c.calls, lines):
                for (path, line), symbol in resolve(c, call).items():
                    if (path, line) != (c.path, c.def_line):
                        graph._add((c.path, c.def_line, c.symbol), (path, line, symbol), at)
        return graph


# ------------------------------------------------------------------ index
def _names_column(chunk):
    path_words = ' '.join(WORD.findall(Path(chunk.path).name.replace('_', ' ')))
    symbol_words = ' '.join(p for part in chunk.symbol.split('.') for p in split_identifier(part))
    return f'{chunk.symbol} {symbol_words} {path_words}'


def _is_test(path):
    """Test code: listed after the code under test, so real callers are not crowded out by the NEIGHBOURS cap."""
    p = Path(path)
    return p.name.lower().startswith('test_') or p.stem.lower().endswith('_test') or any(
        part.lower() in ('test', 'tests', '__tests__', 'spec') for part in p.parts[:-1])


def _sig(st):
    return f'{st.st_size}:{st.st_mtime_ns}'


class CodeIndex:
    """``read_only``: for the MCP server (never creates or changes the file; raises CodeIndexMissing when there is none)."""

    def __init__(self, db_path, read_only=False):
        self.db_path = Path(db_path)
        self.read_only = read_only
        if read_only:
            if not self.db_path.exists():
                raise CodeIndexMissing('There is no code index yet.')
            # A normal connection that never writes: SQLite's mode=ro cannot open a WAL database whose -shm file is gone.
            self.con = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
            row = self.con.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if self._has_meta() else None
            if not row or row[0] != SCHEMA:
                self.con.close()
                raise CodeIndexMissing('The code index was made by a different version of Juno.')
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
            try:
                self._prepare()
            except sqlite3.DatabaseError:
                # Not a database (or damaged): it holds nothing that cannot be rebuilt, so start a new one.
                self.con.close()
                remove(self.db_path)
                self.con = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
                try:
                    self._prepare()
                except Exception:
                    self.con.close()
                    raise
        self._matrix = None
        self._graph = None
        self._version = None

    def _prepare(self):
        self.con.execute('PRAGMA journal_mode=WAL')  # the MCP server can read while the app writes
        self.con.executescript(DDL)
        row = self.con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row and row[0] != SCHEMA:  # an older layout: start over
            for table in ('files', 'chunks', 'fts', 'vectors', 'meta'):
                self.con.execute(f'DELETE FROM {table}')
        self.con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('schema', SCHEMA))
        self.con.commit()

    def _has_meta(self):
        return bool(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone())

    def close(self):
        self.con.close()

    def _fresh(self):
        """Drop cached vectors and graph when another connection changed the file (the app re-indexed)."""
        version = self.con.execute('PRAGMA data_version').fetchone()[0]
        if version != self._version:
            self._version, self._matrix, self._graph = version, None, None

    # ------------------------------------------------------------ build (the app only)
    def update_root(self, root, files, stopping=lambda: False):
        """Bring one tracked folder up to date. ``files``: [(path, stat)] of its code files. Re-reads only files whose
        size or modification time changed; drops files of this root that are gone. Returns (changed, removed)."""
        root = str(root)
        known = dict(self.con.execute('SELECT path, sig FROM files WHERE root=?', (root,)))
        present = {str(p): st for p, st in files if st.st_size <= MAX_FILE_BYTES and is_code(p)}
        removed = [p for p in known if p not in present]
        changed = [p for p, st in present.items() if known.get(p) != _sig(st)]
        for path in removed:
            self._drop_file(path)
        done = 0
        for path in changed:
            if stopping():
                break
            self._drop_file(path)
            try:
                source = read_source(path)
            except OSError:
                continue
            for chunk in chunks_of(path, source) if source else []:
                cur = self.con.execute('INSERT INTO chunks(path, start, end, symbol, kind, text, def_line, calls) VALUES(?,?,?,?,?,?,?,?)',
                                       (path, chunk.start, chunk.end, chunk.symbol, chunk.kind, chunk.text, chunk.def_line,
                                        json.dumps([[n, at] for n, at in zip(chunk.calls, chunk.call_lines)] if chunk.call_lines
                                                   else chunk.calls)))
                self.con.execute('INSERT INTO fts(rowid, text, names) VALUES(?,?,?)',
                                 (cur.lastrowid, chunk.text + '\n' + code_words(chunk.text), _names_column(chunk)))
            self.con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?)', (path, root, _sig(present[path])))
            done += 1
            if done % 50 == 0:
                self.con.commit()
        self.con.commit()
        return done, len(removed)

    def keep_roots(self, roots):
        """Drop every file whose root is not in ``roots`` (a folder was untracked)."""
        keep = {str(r) for r in roots}
        gone = [p for p, r in self.con.execute('SELECT path, root FROM files') if r not in keep]
        for path in gone:
            self._drop_file(path)
        self.con.commit()
        return len(gone)

    def _drop_file(self, path):
        ids = [r[0] for r in self.con.execute('SELECT id FROM chunks WHERE path=?', (path,))]
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            marks = ','.join('?' * len(batch))
            self.con.execute(f'DELETE FROM fts WHERE rowid IN ({marks})', batch)
            self.con.execute(f'DELETE FROM vectors WHERE id IN ({marks})', batch)
        self.con.execute('DELETE FROM chunks WHERE path=?', (path,))
        self.con.execute('DELETE FROM files WHERE path=?', (path,))

    def missing_vectors(self):
        return self.con.execute('SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT id FROM vectors)').fetchone()[0]

    def embed_missing(self, embedder, progress=lambda done, total: None, stopping=lambda: False):
        """Vectors for chunks that have none (all of them again after a model change). Returns how many were embedded."""
        signature = embedder.signature()
        stored = self.con.execute("SELECT value FROM meta WHERE key='vector_signature'").fetchone()
        if stored and stored[0] != signature:
            self.con.execute('DELETE FROM vectors')
        self.con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('vector_signature', signature))
        self.con.commit()
        todo = self.con.execute('SELECT id, path, symbol, text FROM chunks WHERE id NOT IN (SELECT id FROM vectors) ORDER BY id').fetchall()
        if not todo:
            return 0
        plan = getattr(embedder, 'plan', None)
        batch = max(1, min(EMBED_BATCH, (plan(len(todo)) if plan is not None else EMBED_BATCH) or EMBED_BATCH))
        done = 0
        for i in range(0, len(todo), batch):
            if stopping():
                break
            part = todo[i:i + batch]
            vectors = embedder.embed_documents([t for _, _, _, t in part], [f'{Path(p).name} {s}'.strip() for _, p, s, _ in part])
            self.con.executemany('INSERT OR REPLACE INTO vectors VALUES(?,?)',
                                 [(cid, np.asarray(v, dtype=np.float32).tobytes()) for (cid, _, _, _), v in zip(part, vectors)])
            self.con.commit()
            done += len(part)
            progress(done, len(todo))
        return done

    # ------------------------------------------------------------ search
    def graph(self):
        self._fresh()
        if self._graph is None:
            rows = self.con.execute("SELECT path, start, end, symbol, kind, def_line, calls FROM chunks WHERE kind IN ('function','class')")
            chunks = []
            for p, s, e, sym, k, d, c in rows:
                stored = json.loads(c or '[]')  # [[name, line], ...] (or plain names)
                names = [x[0] if isinstance(x, list) else x for x in stored]
                lines = [x[1] if isinstance(x, list) else 0 for x in stored]
                chunks.append(Chunk(p, s, e, sym, k, '', d, names, lines))
            self._graph = CallGraph.build(chunks)
        return self._graph

    def _vectors(self):
        self._fresh()
        if self._matrix is None:
            rows = self.con.execute('SELECT id, vector FROM vectors ORDER BY id').fetchall()
            if rows:
                self._matrix = (np.array([r[0] for r in rows], dtype=np.int64),
                                np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]))
            else:
                self._matrix = (np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32))
        return self._matrix

    @staticmethod
    def _like(path):
        return '%' + path.replace('\\', '/').replace('%', '').replace('_', '\\_') + '%'

    def _path_clause(self, path):
        # Stored paths keep Windows backslashes; match the filter against both spellings.
        return "REPLACE(chunks.path, '\\', '/') LIKE ? ESCAPE '\\'", self._like(path)

    def keyword_ranks(self, query, path=None, limit=CANDIDATES):
        terms = query_terms(query)
        if not terms:
            return []
        sql = 'SELECT fts.rowid FROM fts JOIN chunks ON chunks.id = fts.rowid WHERE fts MATCH ?'
        args = [' OR '.join(f'"{t}"' for t in terms)]
        if path:
            clause, value = self._path_clause(path)
            sql += ' AND ' + clause
            args.append(value)
        sql += ' ORDER BY bm25(fts, 1.0, 3.0) LIMIT ?'
        args.append(limit)
        return [r[0] for r in self.con.execute(sql, args)]

    def meaning_ranks(self, query_vector, path=None, limit=CANDIDATES):
        ids, matrix = self._vectors()
        if query_vector is None or not len(ids) or matrix.shape[1] != len(query_vector):
            return []
        scores = matrix @ np.asarray(query_vector, dtype=np.float32)
        if path:
            clause, value = self._path_clause(path)
            allowed = {r[0] for r in self.con.execute(f'SELECT id FROM chunks WHERE {clause}', (value,))}
            scores = np.where(np.array([i in allowed for i in ids.tolist()]), scores, -np.inf)
        order = np.argsort(-scores)[:limit]
        return [int(ids[i]) for i in order if np.isfinite(scores[i])]

    def exact_ids(self, query, path=None):
        out = []
        for name in named_symbols(query):
            sql = "SELECT id FROM chunks WHERE kind IN ('function','class') AND (symbol = ? OR symbol LIKE ? ESCAPE '\\')"
            args = [name, '%.' + name.replace('_', '\\_')]
            if path:
                clause, value = self._path_clause(path)
                sql += ' AND ' + clause
                args.append(value)
            out.extend(r[0] for r in self.con.execute(sql + ' ORDER BY start', args))
        return out

    def search(self, query, limit=8, query_vector=None, path=None):
        """Ranked chunks: [{path, lines, symbol, kind, matched_by, snippet, callers, callees}]. ``query_vector``: the
        embedder's vector for the query (None: keyword and names only)."""
        lists = [('keyword', self.keyword_ranks(query, path)), ('meaning', self.meaning_ranks(query_vector, path))]
        scores, signals = {}, {}
        for signal, ranked in lists:
            for rank, cid in enumerate(ranked):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                signals.setdefault(cid, set()).add(signal)
        for cid in self.exact_ids(query, path):
            scores[cid] = scores.get(cid, 0.0) + EXACT_BOOST
            signals.setdefault(cid, set()).add('name')
        graph = self.graph()
        results, seen = [], set()
        for cid in sorted(scores, key=lambda c: -scores[c]):
            row = self.con.execute('SELECT path, start, end, symbol, kind, text, def_line FROM chunks WHERE id=?', (cid,)).fetchone()
            if row is None:
                continue
            path_, start, end, symbol, kind, text, def_line = row
            key = (path_, symbol) if symbol else (path_, start)
            if key in seen:
                continue
            seen.add(key)
            result = {'path': path_, 'lines': f'{start}-{end}', 'symbol': symbol or None, 'kind': kind,
                      'matched_by': sorted(signals[cid]), 'snippet': '\n'.join(text.splitlines()[:SNIPPET_LINES])[:SNIPPET_CHARS]}
            if def_line and def_line != start:  # a window of a long function, or a decorated one: where its def is
                result['def_line'] = def_line
            if def_line:
                callers, callees = (sorted(found, key=lambda n: _is_test(n[0])) for found in graph.neighbours(path_, def_line))
                result['callers'] = [self._label(p, line, sym) for p, line, sym in callers[:NEIGHBOURS]]
                result['callees'] = [self._label(p, line, sym) for p, line, sym in callees[:NEIGHBOURS]]
                if len(callers) > NEIGHBOURS:
                    result['more_callers'] = len(callers) - NEIGHBOURS
                if len(callees) > NEIGHBOURS:
                    result['more_callees'] = len(callees) - NEIGHBOURS
            results.append(result)
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _label(path, line, symbol):
        return f'{symbol} ({path}:{line})'

    def stats(self):
        c = self.con
        return {'files': c.execute('SELECT COUNT(*) FROM files').fetchone()[0],
                'chunks': c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],
                'vectors': c.execute('SELECT COUNT(*) FROM vectors').fetchone()[0]}


def remove(db_path):
    """Delete the code index (search_code turned off). True when there was one."""
    found = False
    for suffix in ('', '-wal', '-shm'):
        target = Path(str(db_path) + suffix)
        try:
            target.unlink()
            found = True
        except FileNotFoundError:
            pass
    return found
