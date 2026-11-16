"""Code search tests: chunks, words, the call graph, the code index, the code layer of Index.update, and search_code
in the MCP server.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
No model: meaning search uses a fake embedder. Every ALFRED_HOME is a temporary folder.
"""
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from alfred import code_search as cs, config, mcp_server  # noqa: E402
from alfred.index import Index  # noqa: E402
from test_mcp import McpBase, call  # noqa: E402

SAMPLE = textwrap.dedent('''\
    """Module docstring: storage helpers."""
    import os

    LIMIT = 5


    class IndexBusy(RuntimeError):
        """Raised when indexing already runs."""


    class Store:
        """Keeps passages."""
        size = 3

        def save(self, item):
            self._check(item)
            return write_file(item)

        def _check(self, item):
            if not item:
                raise IndexBusy('busy')


    def write_file(item):
        return os.path.join('x', item)


    @decorated
    def parseHTTPHeader(raw):
        return raw.split(':')
    ''')

CONSENT = textwrap.dedent('''\
    def ask_user(name):
        return True


    def allow_client(name):
        """Decide whether a client may connect; ask the first time."""
        return ask_user(name)
    ''')


class WordEmbedder:
    """Bag-of-words vectors over a small vocabulary, unit length."""
    VOCAB = ['vector', 'embed', 'passage', 'consent', 'allow', 'client', 'hash', 'file', 'secret', 'busy', 'header', 'parse',
             'save', 'write', 'store', 'join', 'path', 'raise', 'item', 'check']
    batch = 8

    def __init__(self, name='v1'):
        self.name = name
        self.calls = 0

    def signature(self):
        return f'words:{self.name}'

    def _vec(self, text):
        words = cs.query_terms(text)
        v = np.array([sum(w.startswith(t) for w in words) for t in self.VOCAB], dtype=np.float32) + 1e-3
        return (v / np.linalg.norm(v)).tolist()

    def embed_documents(self, texts, titles=None):
        self.calls += 1
        return [self._vec(f'{ti} {t}') for t, ti in zip(texts, titles or [''] * len(texts))]

    def embed_query(self, text):
        return self._vec(text)


class WordsTests(unittest.TestCase):
    def test_split_identifier(self):
        self.assertEqual(cs.split_identifier('_embed_missing'), ['embed', 'missing'])
        self.assertEqual(cs.split_identifier('IndexBusy'), ['index', 'busy'])
        self.assertEqual(cs.split_identifier('parseHTTPHeader'), ['parse', 'http', 'header'])
        self.assertEqual(cs.split_identifier('HTTPServer2'), ['http', 'server', '2'])
        self.assertEqual(cs.split_identifier('__init__'), ['init'])

    def test_code_words_adds_camel_parts_only(self):
        words = cs.code_words('x = parseHTTPHeader(raw); snake_case_name; ALLCAPS').split()
        self.assertIn('parse', words)
        self.assertNotIn('snake', words)  # FTS already splits on '_'
        self.assertNotIn('allcaps', words)

    def test_query_terms(self):
        self.assertEqual(cs.query_terms('Where does the IndexBusy error get raised?'), ['index', 'busy', 'error', 'get', 'raised'])
        self.assertEqual(cs.query_terms('what calls _embed_missing'), ['embed', 'missing'])
        self.assertEqual(cs.query_terms('the of and'), [])

    def test_named_symbols(self):
        self.assertEqual(cs.named_symbols('who calls _embed_missing?'), {'_embed_missing'})
        self.assertEqual(cs.named_symbols('where is IndexBusy raised'), {'IndexBusy'})
        self.assertEqual(cs.named_symbols('what does Index.search return'), {'search'})
        self.assertEqual(cs.named_symbols('explain release() please'), {'release'})
        self.assertEqual(cs.named_symbols('how does search work'), set())

    def test_is_code(self):
        self.assertTrue(cs.is_code(r'C:\work\app\main.py'))
        self.assertTrue(cs.is_code('/w/web/App.TSX'))
        self.assertFalse(cs.is_code(r'C:\work\app\notes.md'))
        self.assertFalse(cs.is_code(r'C:\work\app\node_modules\lib\x.js'))
        self.assertFalse(cs.is_code(r'C:\work\app\.venv\Lib\site-packages\numpy\core.py'))


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.chunks = cs.python_chunks('store.py', SAMPLE)
        self.by = {c.symbol: c for c in self.chunks if c.symbol}

    def test_symbols_kinds_and_lines(self):
        self.assertEqual(set(self.by), {'IndexBusy', 'Store', 'Store.save', 'Store._check', 'write_file', 'parseHTTPHeader'})
        self.assertEqual(self.by['Store'].kind, 'class')
        save = self.by['Store.save']
        self.assertTrue(SAMPLE.splitlines()[save.start - 1].strip().startswith('def save'))
        self.assertEqual((save.end - save.start, save.def_line), (2, save.start))

    def test_decorator_belongs_to_function(self):
        parse = self.by['parseHTTPHeader']
        self.assertEqual(SAMPLE.splitlines()[parse.start - 1].strip(), '@decorated')
        self.assertEqual(parse.def_line, parse.start + 1)

    def test_class_chunk_is_header_with_members(self):
        self.assertIn('size = 3', self.by['Store'].text)
        self.assertIn('# members: save, _check', self.by['Store'].text)
        self.assertNotIn('return write_file', self.by['Store'].text)

    def test_top_level_code_is_chunked(self):
        top = [c for c in self.chunks if not c.symbol]
        self.assertTrue(any('Module docstring' in c.text and c.kind == 'module' for c in top))
        self.assertTrue(any('LIMIT = 5' in c.text for c in top))

    def test_calls(self):
        self.assertEqual(self.by['Store.save'].calls, ['self._check', 'write_file'])
        self.assertIn('IndexBusy', self.by['Store._check'].calls)
        self.assertIn('path.join', self.by['write_file'].calls)

    def test_long_function_windows_cover_every_line(self):
        body = '\n'.join(f'    x{i} = {i}' for i in range(200))
        chunks = cs.python_chunks('long.py', f'def big():\n{body}\n')
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(c.symbol == 'big' for c in chunks))
        self.assertTrue(chunks[1].text.startswith('def big():\n    ...'))
        covered = set()
        for c in chunks:
            covered.update(range(c.start, c.end + 1))
        self.assertEqual(covered, set(range(1, 202)))

    def test_syntax_error_and_other_languages_use_windows(self):
        self.assertTrue(all(c.kind == 'block' for c in cs.chunks_of('bad.py', 'def broken(:\n    pass\n' * 40)))
        js = '\n'.join(f'function f{i}() {{ return {i}; }}' for i in range(130))
        self.assertEqual([(c.start, c.end) for c in cs.chunks_of('app.js', js)], [(1, 60), (51, 110), (101, 130)])
        self.assertEqual(cs.chunks_of('e.py', ''), [])


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.root = self.dir / 'repo'
        (self.root / 'pkg').mkdir(parents=True)
        self.write('pkg/store.py', SAMPLE)
        self.write('pkg/consent.py', CONSENT)
        self.code = cs.CodeIndex(self.dir / 'code.sqlite')

    def tearDown(self):
        self.code.close()
        self.tmp.cleanup()

    def write(self, rel, text):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return str(target)

    def files(self):
        out = []
        for folder, _, names in os.walk(self.root):
            for name in names:
                full = os.path.join(folder, name)
                out.append((full, os.stat(full)))
        return out

    def update(self, embedder=None):
        result = self.code.update_root(self.root, self.files())
        if embedder is not None:
            self.code.embed_missing(embedder)
        return result

    def find(self, query, **kw):
        embedder = kw.pop('embedder', None)
        vector = embedder.embed_query(query) if embedder else None
        return self.code.search(query, query_vector=vector, **kw)

    def one(self, query, symbol, **kw):
        return [r for r in self.find(query, limit=20, **kw) if r['symbol'] == symbol][0]


class CodeIndexTests(Folder):
    def test_incremental_update(self):
        self.write('README.md', 'not code')
        self.assertEqual(self.update(), (2, 0))
        self.assertEqual(self.update(), (0, 0))
        time.sleep(0.01)
        self.write('pkg/consent.py', CONSENT + '\n\ndef revoke_client(name):\n    return False\n')
        self.assertEqual(self.update(), (1, 0))
        self.assertTrue(self.find('revoke_client'))
        os.remove(self.root / 'pkg' / 'consent.py')
        self.assertEqual(self.update(), (0, 1))
        self.assertFalse([r for r in self.find('revoke client') if r['path'].endswith('consent.py')])
        con = self.code.con
        self.assertEqual(con.execute('SELECT COUNT(*) FROM fts').fetchone()[0], con.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])

    def test_binary_and_installed_code_skipped(self):
        (self.root / 'pkg' / 'blob.py').write_bytes(b'\0\0binary')
        self.write('node_modules/lib/x.js', 'function vendored() {}')
        self.update()
        self.assertEqual(self.code.con.execute("SELECT COUNT(*) FROM chunks WHERE path LIKE '%blob.py'").fetchone()[0], 0)
        self.assertFalse(any(p.endswith('x.js') for (p,) in self.code.con.execute('SELECT path FROM files')))

    def test_paths_are_absolute(self):
        self.update()
        result = self.find('IndexBusy')[0]
        self.assertEqual(result['path'], str(self.root / 'pkg' / 'store.py'))
        self.assertTrue(os.path.isabs(result['path']))

    def test_keyword_finds_split_identifiers(self):
        self.update()
        self.assertEqual(self.find('index busy')[0]['symbol'], 'IndexBusy')
        self.assertEqual(self.find('parse header')[0]['symbol'], 'parseHTTPHeader')

    def test_exact_name_wins(self):
        self.update()
        first = self.find('where is write_file used')[0]
        self.assertEqual(first['symbol'], 'write_file')
        self.assertIn('name', first['matched_by'])
        self.assertEqual(self.find('Store._check')[0]['symbol'], 'Store._check')

    def test_path_filter_accepts_either_slash(self):
        self.update()
        for spelling in ('pkg/consent', 'pkg\\consent', 'consent.py'):
            results = self.find('client', path=spelling)
            self.assertTrue(results, spelling)
            self.assertTrue(all(r['path'].endswith('consent.py') for r in results), spelling)
        self.assertEqual(self.find('client', path='nowhere/'), [])

    def test_underscore_in_path_filter_is_literal(self):
        self.write('a_b/one.py', 'def one():\n    pass\n')
        self.write('axb/two.py', 'def one_more():\n    pass\n')
        self.update()
        self.assertTrue(all('a_b' in r['path'] for r in self.find('one', path='a_b')))

    def test_nothing_to_find(self):
        self.update()
        self.assertEqual(self.find('the of and'), [])
        self.assertEqual(self.find('zzzqqq'), [])

    def test_meaning_and_hybrid(self):
        embedder = WordEmbedder()
        self.update(embedder)
        self.assertEqual(self.code.missing_vectors(), 0)
        top = self.code.search('may this client connect? consent', query_vector=embedder.embed_query('consent client'))[0]
        self.assertTrue(top['path'].endswith('consent.py'))
        hybrid = self.find('consent for a client', embedder=embedder)
        self.assertEqual(hybrid[0]['symbol'], 'allow_client')
        self.assertIn('meaning', hybrid[0]['matched_by'])
        self.assertEqual(self.code.embed_missing(embedder), 0)  # nothing new
        total = self.code.stats()['chunks']
        self.assertEqual(self.code.embed_missing(WordEmbedder('v2')), total)  # another model: everything again

    def test_wrong_vector_size_is_ignored(self):
        self.update(WordEmbedder())
        self.assertTrue(self.code.search('IndexBusy', query_vector=[0.1, 0.2]))

    def test_results_unique_and_bounded(self):
        body = '\n'.join(f'    total_sum_{i} = {i}' for i in range(200))
        self.write('pkg/long.py', f'def total_sum():\n{body}\n')
        self.update()
        results = self.find('total sum', limit=10)
        keys = [(r['path'], r['symbol']) for r in results]
        self.assertEqual(len(keys), len(set(keys)))
        for r in results:
            self.assertLessEqual(len(r['snippet']), cs.SNIPPET_CHARS)

    def test_keep_roots(self):
        self.update()
        self.assertEqual(self.code.keep_roots([str(self.root)]), 0)
        self.assertEqual(self.code.keep_roots([]), 2)
        self.assertEqual(self.code.stats()['chunks'], 0)

    def test_read_only_open(self):
        with self.assertRaises(cs.CodeIndexMissing):
            cs.CodeIndex(self.dir / 'none.sqlite', read_only=True)
        self.assertFalse((self.dir / 'none.sqlite').exists())
        self.update()
        reader = cs.CodeIndex(self.dir / 'code.sqlite', read_only=True)
        try:
            self.assertEqual(reader.search('IndexBusy')[0]['symbol'], 'IndexBusy')
            # The reader sees what the writer adds later (its caches follow the file).
            self.write('pkg/late.py', 'def latecomer():\n    allow_client("x")\n')
            self.update()
            self.assertEqual(reader.search('latecomer')[0]['symbol'], 'latecomer')
            self.assertEqual(reader.search('allow_client')[0]['callers'][0].split(' ')[0], 'latecomer')
        finally:
            reader.close()

    def test_other_schema_is_refused_by_reader_and_reset_by_writer(self):
        self.update()
        self.code.con.execute("UPDATE meta SET value='code-v0' WHERE key='schema'")
        self.code.con.commit()
        with self.assertRaises(cs.CodeIndexMissing):
            cs.CodeIndex(self.dir / 'code.sqlite', read_only=True)
        self.code.close()
        self.code = cs.CodeIndex(self.dir / 'code.sqlite')
        self.assertEqual(self.code.stats()['chunks'], 0)

    def test_remove(self):
        self.update()
        self.code.close()
        self.assertTrue(cs.remove(self.dir / 'code.sqlite'))
        self.assertFalse((self.dir / 'code.sqlite').exists())
        self.assertFalse(cs.remove(self.dir / 'code.sqlite'))
        self.code = cs.CodeIndex(self.dir / 'code.sqlite')


class GraphTests(Folder):
    def test_plain_and_self_calls(self):
        self.update()
        save = self.one('Store.save', 'Store.save')
        self.assertTrue(any(c.startswith('Store._check ') for c in save['callees']))
        self.assertTrue(any(c.startswith('write_file ') for c in save['callees']))
        self.assertEqual(self.one('allow_client', 'allow_client')['callees'], [f"ask_user ({self.root / 'pkg' / 'consent.py'}:1)"])
        self.assertEqual([c.split(' ')[0] for c in self.one('ask_user', 'ask_user')['callers']], ['allow_client'])

    def test_receiver_rules(self):
        self.write('pkg/engine.py', textwrap.dedent('''\
            class SearchIndex:
                def search(self, q):
                    return self.rank(q)

                def rank(self, q):
                    return q


            class Lock:
                def release(self):
                    pass


            def release():
                pass
            '''))
        self.write('pkg/front.py', textwrap.dedent('''\
            import engine


            def handler(search_index, lock, thing):
                search_index.search('x')
                engine.release()
                thing.search('y')
                lock.release()
                compute().search('z')
            '''))
        self.update()
        callees = sorted(c.split(' ')[0] for c in self.one('handler', 'handler', path='front.py')['callees'])
        self.assertEqual(callees, ['Lock.release', 'SearchIndex.search', 'release'])
        self.assertEqual([c.split(' ')[0] for c in self.one('SearchIndex.search', 'SearchIndex.search')['callees']], ['SearchIndex.rank'])

    def test_unknown_receivers_and_ambiguous_names_give_no_edge(self):
        self.write('pkg/a.py', 'def helper():\n    pass\n')
        self.write('pkg/b.py', 'def helper():\n    pass\n')
        self.write('pkg/c.py', 'def user():\n    helper()\n')
        self.write('pkg/lockuser.py', 'class Runner:\n    def stop(self):\n        self._lock.release()\n')
        self.write('pkg/rt.py', 'def release():\n    pass\n')
        self.update()
        self.assertEqual(self.one('def user', 'user', path='c.py')['callees'], [])
        self.assertEqual(self.one('Runner.stop', 'Runner.stop')['callees'], [])

    def test_same_file_definition_preferred(self):
        self.write('pkg/a.py', 'def helper():\n    pass\n\n\ndef user():\n    helper()\n')
        self.write('pkg/b.py', 'def helper():\n    pass\n')
        self.update()
        self.assertEqual(self.one('user', 'user', path='a.py')['callees'], [f"helper ({self.root / 'pkg' / 'a.py'}:1)"])

    def test_callers_point_at_the_call_and_callees_at_the_definition(self):
        self.write('pkg/flow.py', 'def worker():\n    pass\n\n\ndef boss():\n    x = 1\n    y = 2\n    worker()\n')
        self.update()
        flow = self.root / 'pkg' / 'flow.py'
        self.assertEqual(self.one('worker', 'worker', path='flow.py')['callers'], [f'boss ({flow}:8)'])  # the call
        self.assertEqual(self.one('boss', 'boss', path='flow.py')['callees'], [f'worker ({flow}:1)'])  # the def

    def test_callers_outside_tests_come_first(self):
        self.write('pkg/core.py', 'def target():\n    pass\n')
        for i in range(8):
            self.write(f'tests/test_{i}.py', f'from core import target\n\n\ndef test_{i}():\n    target()\n')
        self.write('pkg/app.py', 'def main():\n    target()\n')
        self.update()
        callers = self.one('target', 'target', path='core.py')['callers']
        self.assertEqual(callers[0].split(' ')[0], 'main')
        self.assertEqual(self.one('target', 'target', path='core.py')['more_callers'], 9 - cs.NEIGHBOURS)

    def test_window_of_long_function_names_its_def_line(self):
        body = '\n'.join(f'    step_{i} = {i}' for i in range(150))
        self.write('pkg/long.py', f'@wrap\ndef very_long():\n{body}\n    return final_answer_marker\n')
        self.update()
        hit = self.find('final_answer_marker')[0]
        self.assertEqual(hit['symbol'], 'very_long')
        self.assertNotEqual(hit['lines'].split('-')[0], '1')
        self.assertEqual(hit['def_line'], 2)

    def test_neighbour_cap(self):
        calls = '\n'.join(f'def c{i}():\n    target()\n' for i in range(10))
        self.write('pkg/many.py', 'def target():\n    pass\n\n' + calls)
        self.update()
        target = self.one('target', 'target', path='many.py')
        self.assertEqual(len(target['callers']), cs.NEIGHBOURS)
        self.assertEqual(target['more_callers'], 10 - cs.NEIGHBOURS)


class LayerTests(unittest.TestCase):
    """The code layer of Index.update."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.folder = self.dir / 'Tracked'
        (self.folder / 'src').mkdir(parents=True)
        (self.folder / 'src' / 'store.py').write_text(SAMPLE, encoding='utf-8')
        (self.folder / 'notes.txt').write_text('harbour pilot notes', encoding='utf-8')
        self.settings = config.load(self.dir / 'home')
        self.settings['tracked_folders'] = [str(self.folder)]
        self.index = Index(self.settings)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def chunks(self):
        con = sqlite3.connect(self.settings.code_db_path)
        try:
            return con.execute('SELECT COUNT(*), COUNT(DISTINCT path) FROM chunks').fetchone()
        finally:
            con.close()

    def test_on_by_default_and_built_with_the_index(self):
        self.assertTrue(config.DEFAULTS['code_search'])
        summary = self.index.update(embedder=WordEmbedder())
        self.assertEqual(summary['code_files'], 1)
        self.assertGreater(summary['code_embedded'], 0)
        count, files = self.chunks()
        self.assertGreater(count, 5)
        self.assertEqual(files, 1)
        # File search is unchanged: the text file is still found.
        self.assertTrue(self.index.search('harbour pilot'))

    def test_keyword_only_without_embedder(self):
        summary = self.index.update()
        self.assertEqual((summary['code_files'], summary['code_embedded']), (1, 0))
        reader = cs.CodeIndex(self.settings.code_db_path, read_only=True)
        try:
            self.assertEqual(reader.search('IndexBusy')[0]['symbol'], 'IndexBusy')
        finally:
            reader.close()

    def test_turned_off_deletes_the_code_index(self):
        self.index.update()
        self.assertTrue(self.settings.code_db_path.exists())
        self.settings['code_search'] = False
        summary = self.index.update()
        self.assertFalse(self.settings.code_db_path.exists())
        self.assertEqual(summary['code_files'], 0)

    def test_untracked_folder_leaves_the_code_index(self):
        self.index.update()
        self.settings['tracked_folders'] = []
        self.index.remove_root(self.folder)
        self.assertEqual(self.chunks(), (0, 0))

    def test_a_damaged_code_index_is_rebuilt(self):
        self.settings.code_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.code_db_path.write_bytes(b'this is not a database' * 100)
        summary = self.index.update()
        self.assertEqual(summary['code_files'], 1)
        self.assertGreater(self.chunks()[0], 0)

    def test_a_failing_code_layer_does_not_stop_indexing(self):
        from unittest import mock
        with mock.patch.object(cs.CodeIndex, 'update_root', side_effect=RuntimeError('disk trouble')):
            summary = self.index.update()
        self.assertTrue(any('Code search could not be updated: disk trouble' in w for w in summary['warnings']))
        self.assertTrue(self.index.search('harbour pilot'))


class McpCodeTests(McpBase):
    def setUp(self):
        super().setUp()
        (self.folder / 'store.py').write_text(SAMPLE, encoding='utf-8')
        self.index.update()

    def tearDown(self):
        if self.server.code is not None:
            self.server.code.close()
        super().tearDown()

    def test_listed_and_described_when_on(self):
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'})['result']['tools']
        self.assertEqual([t['name'] for t in tools], ['search_files', 'read_passage', 'search_code'])
        self.assertIn('search_code', self.hello()['result']['instructions'])

    def test_search_code(self):
        data = self.payload(self.tool('search_code', {'query': 'IndexBusy'}))
        first = data['results'][0]
        self.assertEqual((first['symbol'], first['path']), ('IndexBusy', str(self.folder / 'store.py')))
        self.assertIn('callers', first)
        self.assertNotIn('_files', data)
        entry = mcp_server.recent_calls(self.settings.home)[-1]
        self.assertEqual((entry['tool'], entry['query'], entry['outcome']), ('search_code', 'IndexBusy', 'ok'))
        self.assertEqual(entry['files'], [str(self.folder / 'store.py')])

    def test_no_match_note(self):
        data = self.payload(self.tool('search_code', {'query': 'zzzqqq'}))
        self.assertEqual(data['results'], [])
        self.assertTrue(any('No matching code' in n for n in data['notes']))

    def test_bad_arguments(self):
        for arguments in ({}, {'query': ''}, {'query': 5}, {'query': 'x', 'limit': 0}, {'query': 'x', 'limit': 99},
                          {'query': 'x', 'other': 1}, {'query': 'x' * 501}, {'query': 'x', 'path': 3}):
            result = self.server.handle(call('search_code', arguments))['result']
            self.assertTrue(result['isError'], arguments)

    def test_off_hides_the_tool_and_refuses(self):
        self.settings['code_search'] = False
        self.settings.save()
        tools = self.server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'})['result']['tools']
        self.assertNotIn('search_code', [t['name'] for t in tools])
        self.assertNotIn('search_code', self.hello()['result']['instructions'])
        result = self.tool('search_code', {'query': 'IndexBusy'})
        self.assertTrue(result['isError'])
        self.assertIn('turned off', result['content'][0]['text'])

    def test_missing_code_index(self):
        cs.remove(self.settings.code_db_path)
        result = self.tool('search_code', {'query': 'IndexBusy'})
        self.assertTrue(result['isError'])
        self.assertIn('no code index', result['content'][0]['text'].lower())
        self.assertFalse(self.settings.code_db_path.exists())  # the server never creates it

    def test_consent_applies(self):
        self.answer = False
        self.hello('another-client')
        result = self.tool('search_code', {'query': 'IndexBusy'})
        self.assertTrue(result['isError'])

    def test_meaning_through_the_embedder(self):
        self.index.update(embedder=WordEmbedder())
        self.server.embedder = WordEmbedder()
        data = self.payload(self.tool('search_code', {'query': 'store an item and write it'}))
        self.assertTrue(any('meaning' in r['matched_by'] for r in data['results']))


if __name__ == '__main__':
    unittest.main()
