"""Search tests: per-search overhead (shared read connections, cached roots, the result cache),
identifier-aware ranking, and the relevance floor ("nothing found" instead of weak matches).

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import gc
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alfred import index as index_module  # noqa: E402
from alfred.index import connect  # noqa: E402
from test_pipeline import Base, FakeEmbedder  # noqa: E402


class OverheadTests(Base):
    def setUp(self):
        super().setUp()
        for number in range(12):
            (self.folder / f'note{number}.txt').write_text(f'harbour pilot schedule number {number}', encoding='utf-8')
        (self.folder / 'sub').mkdir()
        (self.folder / 'sub' / 'deep.txt').write_text('harbour pilot schedule in a subfolder', encoding='utf-8')
        self.index.update()

    def test_no_real_path_lookups_per_result(self):
        self.index.search('harbour pilot')  # first search resolves the tracked folders once
        real = os.path.realpath
        with mock.patch('os.path.realpath', side_effect=real) as spy:
            results = self.index.search('pilot schedule', limit=20)
        self.assertEqual(len(results), 13)
        self.assertEqual(spy.call_count, 0)
        self.assertIn('Tracked › sub', {r['folder'] for r in results})

    def test_one_read_connection_per_thread(self):
        with mock.patch.object(index_module, 'connect', side_effect=connect) as opened:
            for query in ('harbour', 'pilot', 'schedule', 'subfolder'):
                self.assertTrue(self.index.search(query))
            self.index.retrieve('harbour pilot')
        self.assertEqual(opened.call_count, 0)  # no new connection (and no schema script) per search
        found = []
        worker = threading.Thread(target=lambda: found.extend(self.index.search('subfolder')))
        worker.start()
        worker.join()
        self.assertEqual([r['filename'] for r in found], ['deep.txt'])
        gc.collect()
        self.assertEqual(len(self.index._readers), 1)  # the finished thread's connection was closed with it

    def test_repeated_search_is_remembered_until_the_index_changes(self):
        first = self.index.search('harbour pilot')
        with mock.patch.object(self.index, '_keyword', side_effect=AssertionError('searched again')):
            self.assertEqual(self.index.search('harbour pilot'), first)
        # A new file is found as soon as it is indexed.
        (self.folder / 'extra.txt').write_text('harbour pilot extra notes', encoding='utf-8')
        self.index.update()
        self.assertIn('extra.txt', [r['filename'] for r in self.index.search('harbour pilot', limit=50, per_file=5)])
        # A deleted file is gone from the next search.
        (self.folder / 'extra.txt').unlink()
        self.index.update()
        self.assertNotIn('extra.txt', [r['filename'] for r in self.index.search('harbour pilot', limit=50, per_file=5)])

    def test_cache_sees_changes_from_other_connections_and_folders(self):
        self.assertTrue(self.index.search('subfolder'))
        con = connect(self.index.db_path)
        with con:  # another process (or the indexer) removes the passage
            con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE path LIKE '%deep.txt')")
        con.close()
        self.assertFalse(self.index.search('subfolder'))
        self.assertTrue(self.index.search('harbour'))
        self.settings['tracked_folders'] = []  # untracked: nothing shows, even before the index is cleaned
        self.assertFalse(self.index.search('harbour'))

    def test_results_are_copies(self):
        self.index.search('harbour')[0]['filename'] = 'changed'
        self.assertNotEqual(self.index.search('harbour')[0]['filename'], 'changed')

    def test_clearing_the_query_cache_clears_results(self):
        embedder = FakeEmbedder()
        self.index.update(embedder=embedder)
        self.index.search('harbour', embedder=embedder)
        self.index._query_cache.clear()
        with mock.patch.object(embedder, 'embed_query', wraps=embedder.embed_query) as embed:
            self.index.search('harbour', embedder=embedder)
        self.assertEqual(embed.call_count, 1)


if __name__ == '__main__':
    unittest.main()
