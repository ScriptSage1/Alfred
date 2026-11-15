""""Only new files from now on" (M12): files already in a folder are tracked as 'skipped' (found by name, never read)
until they change; folder_since, "Include older files too", rebuilds, and the settings defaults of M12.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
No model server, GPU or real OCR: OCR and the embedder are fakes.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alfred import ai, config, extract  # noqa: E402
from alfred.index import Index, connect  # noqa: E402
from test_layers import FakeOcr, PictureEmbedder, picture  # noqa: E402
from test_pipeline import Base  # noqa: E402


class SpyEmbedder(PictureEmbedder):
    """PictureEmbedder that remembers every text it embedded."""

    def __init__(self):
        super().__init__('spy')
        self.texts = []

    def embed_documents(self, texts, titles=None):
        self.texts += list(texts)
        return super().embed_documents(texts, titles)


class SkippedTests(Base):
    def setUp(self):
        super().setUp()
        self.ocr = FakeOcr({'harbour-chart.png': 'Tide table for the north pier'}).patch(self)
        self.embedder = SpyEmbedder()
        self.read = mock.patch.object(extract, 'extract', wraps=extract.extract)
        self.extract = self.read.start()
        self.addCleanup(self.read.stop)
        (self.folder / 'lighthouse-rota.txt').write_text('zebra crossing schedule for the winter keepers', encoding='utf-8')
        (self.folder / 'harbour-notes.md').write_text('# Pilot boats\nThe quartermaster logs every departure', encoding='utf-8')
        picture(self.folder / 'harbour-chart.png')
        (self.folder / 'archive.xyz').write_text('not a supported type', encoding='utf-8')

    def read_names(self):
        return sorted(Path(call.args[0]).name for call in self.extract.call_args_list)

    def statuses(self, index=None):
        con = connect((index or self.index).db_path)
        try:
            return {Path(p).name: s for p, s in con.execute('SELECT path, status FROM files')}
        finally:
            con.close()

    def found(self, query, index=None):
        return [r['filename'] for r in (index or self.index).search(query, mode='keyword')]

    def track_only_new(self):
        self.index.track_only_new(self.folder)
        self.settings.save()
        return self.index.update(embedder=self.embedder)

    def test_tracking_only_new_reads_no_content(self):
        summary = self.track_only_new()
        self.assertEqual(self.extract.call_count, 0)
        self.assertEqual((self.ocr.files, self.ocr.pages, self.embedder.pictures), ([], 0, 0))
        self.assertEqual(self.embedder.texts, [])  # name passages of skipped files get no vector
        self.assertEqual(self.statuses(), {'lighthouse-rota.txt': 'skipped', 'harbour-notes.md': 'skipped',
                                           'harbour-chart.png': 'skipped', 'archive.xyz': 'unsupported'})
        self.assertEqual((summary['skipped'], summary['indexed']), (3, 0))
        self.assertEqual(self.found('lighthouse rota'), ['lighthouse-rota.txt'])
        self.assertEqual(self.found('harbour notes'), ['harbour-notes.md'])
        self.assertEqual(self.found('harbour chart'), ['harbour-chart.png'])
        for words in ('zebra crossing', 'quartermaster', 'tide table'):
            self.assertEqual(self.found(words), [])
        with self.assertRaises(ValueError):
            self.index.read_excerpt(self.folder / 'lighthouse-rota.txt')
        self.assertEqual(self.index.layer_status()['meaning'], [0, 0])
        self.assertEqual(self.settings.since_for(self.folder), config.load(self.home).since_for(self.folder))
        stats = self.index.folder_stats()[str(self.folder)]
        self.assertEqual((stats['files'], stats['full'], stats['older'], stats['skipped']), (3, 0, 3, 1))
        self.assertEqual(self.index.status()['older'], 3)

    def test_a_skipped_pdf_is_left_alone_by_every_layer(self):
        import pymupdf
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'The boiler pressure must stay below two bar.', fontsize=12)
            document.save(self.folder / 'boiler-manual.pdf')
        with mock.patch.object(extract, 'table_pages', wraps=extract.table_pages) as tables, \
                mock.patch.object(extract, 'pdf_page_images', wraps=extract.pdf_page_images) as pages:
            self.track_only_new()
            self.index.update(embedder=self.embedder)
        self.assertEqual((self.extract.call_count, tables.call_count, pages.call_count), (0, 0, 0))
        self.assertEqual(self.statuses()['boiler-manual.pdf'], 'skipped')
        self.assertEqual(self.found('boiler manual'), ['boiler-manual.pdf'])
        self.assertEqual(self.found('pressure'), [])
        con = connect(self.index.db_path)
        try:
            self.assertEqual(con.execute('SELECT count(*) FROM pdf_tables').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM layers').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM vectors').fetchone()[0], 0)
        finally:
            con.close()

    def test_passage_contexts_leave_skipped_files_alone(self):
        class Writer:
            situated = []

            def signature(self):
                return 'fake-context'

            def situate(self, title, document, passage):
                self.situated.append(title)
                return 'context'
        with mock.patch.object(ai.Contextualizer, 'from_settings', lambda settings: Writer()):
            self.track_only_new()
            (self.folder / 'new-memo.txt').write_text('a brand new walrus inventory', encoding='utf-8')
            self.index.update(embedder=self.embedder)
        self.assertEqual(Writer.situated, ['new-memo.txt'])

    def test_added_changed_and_moved_in_files_are_read(self):
        self.track_only_new()
        (self.folder / 'new-memo.txt').write_text('a brand new walrus inventory', encoding='utf-8')
        (self.folder / 'lighthouse-rota.txt').write_text('zebra crossing schedule, now with spring shifts', encoding='utf-8')
        moved = self.folder / 'moved-in.txt'
        moved.write_text('an old flamingo ledger copied from a backup', encoding='utf-8')
        old = time.time() - 400 * 86400
        os.utime(moved, (old, old))  # moved in with an old date: still new to the index
        self.index.update(embedder=self.embedder)
        self.assertEqual(self.read_names(), ['lighthouse-rota.txt', 'moved-in.txt', 'new-memo.txt'])
        self.assertEqual(self.found('walrus inventory'), ['new-memo.txt'])
        self.assertEqual(self.found('spring shifts'), ['lighthouse-rota.txt'])
        self.assertEqual(self.found('flamingo ledger'), ['moved-in.txt'])
        self.assertEqual(self.found('quartermaster'), [])
        self.assertEqual(self.statuses()['lighthouse-rota.txt'], 'ok')
        self.assertEqual(self.statuses()['harbour-notes.md'], 'skipped')
        self.assertFalse(any('quartermaster' in text for text in self.embedder.texts))
        self.assertEqual(self.ocr.files, [])

    def test_unchanged_skipped_file_stays_unread(self):
        self.track_only_new()
        self.index.update(embedder=self.embedder)  # a rescan
        self.index.rebuild(embedder=self.embedder)  # "Rebuild index" keeps skipped rows and their name passages
        self.assertEqual(self.extract.call_count, 0)
        self.assertEqual(self.found('harbour notes'), ['harbour-notes.md'])
        self.assertEqual(self.statuses()['harbour-notes.md'], 'skipped')
        self.index.close()
        reopened = Index(config.load(self.home))  # an app restart
        try:
            reopened.update(embedder=self.embedder)
            self.assertEqual(self.extract.call_count, 0)
            self.assertEqual(self.found('lighthouse rota', reopened), ['lighthouse-rota.txt'])
            self.assertEqual(self.found('zebra crossing', reopened), [])
        finally:
            reopened.close()
        self.assertEqual((self.ocr.files, self.embedder.pictures), ([], 0))
        self.assertFalse(any('zebra' in text or 'quartermaster' in text for text in self.embedder.texts))

    def test_index_from_nothing_skips_files_older_than_folder_since(self):
        self.track_only_new()
        self.index.close()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(str(self.settings.db_path) + suffix):
                os.remove(str(self.settings.db_path) + suffix)
        since = time.time() + 3600  # later than every file here (their created time is now)
        self.settings.set_since(self.folder, since)
        self.settings.save()
        later = self.folder / 'harbour-notes.md'
        os.utime(later, (since + 60, since + 60))  # modified after folder_since: read in full
        self.index = Index(config.load(self.home))
        self.index.update(embedder=self.embedder)
        self.assertEqual(self.read_names(), ['harbour-notes.md'])
        self.assertEqual(self.statuses(), {'lighthouse-rota.txt': 'skipped', 'harbour-notes.md': 'ok',
                                           'harbour-chart.png': 'skipped', 'archive.xyz': 'unsupported'})
        self.assertEqual(self.found('quartermaster'), ['harbour-notes.md'])
        added = self.folder / 'later-memo.txt'
        added.write_text('a pelican roster', encoding='utf-8')
        self.index.update(embedder=self.embedder)  # the baseline is recorded now: a file not in the index is new
        self.assertEqual(self.found('pelican roster'), ['later-memo.txt'])

    def test_include_older_files_reads_them_and_clears_the_entry(self):
        self.track_only_new()
        self.assertEqual(self.index.include_older(self.folder), 3)
        self.assertIsNone(self.settings.since_for(self.folder))
        self.assertIsNone(config.load(self.home).since_for(self.folder))
        self.assertEqual(self.index.pending_changes()['changed'], 3)  # pending files count as changed
        self.index.update(embedder=self.embedder)
        self.assertEqual(self.read_names(), ['harbour-chart.png', 'harbour-notes.md', 'lighthouse-rota.txt'])  # the picture's OCR reads it
        self.assertEqual(self.found('quartermaster'), ['harbour-notes.md'])
        self.assertEqual(self.found('zebra crossing'), ['lighthouse-rota.txt'])
        self.assertEqual(self.ocr.files, ['harbour-chart.png'])
        self.assertEqual(self.embedder.pictures, 1)
        stats = self.index.folder_stats()[str(self.folder)]
        self.assertEqual((stats['full'], stats['older']), (3, 0))

    def test_untracking_clears_the_entry(self):
        self.track_only_new()
        variant = str(self.folder).upper() if os.name == 'nt' else str(self.folder)
        self.settings.untrack(variant)  # compared like tracked folders (paths.canonical)
        self.settings.save()
        self.index.remove_root(self.folder)
        loaded = config.load(self.home)
        self.assertEqual((loaded['tracked_folders'], loaded['folder_since']), ([], {}))
        con = connect(self.index.db_path)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM meta WHERE key LIKE 'since_baseline:%'").fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM files').fetchone()[0], 0)
        finally:
            con.close()

    def test_watcher_ignores_unchanged_skipped_files(self):
        self.track_only_new()
        self.assertEqual(self.index.pending_changes(), {'new': 0, 'changed': 0, 'removed': 0})
        (self.folder / 'harbour-notes.md').write_text('# Pilot boats\nThe quartermaster logs every departure and arrival', encoding='utf-8')
        self.assertEqual(self.index.pending_changes(), {'new': 0, 'changed': 1, 'removed': 0})

    def test_everything_folder_is_unchanged(self):
        self.index.update(embedder=self.embedder)
        self.assertEqual(self.read_names(), ['harbour-chart.png', 'harbour-notes.md', 'lighthouse-rota.txt'])  # the picture's OCR reads it
        self.assertNotIn('skipped', self.statuses().values())
        self.assertEqual(self.found('quartermaster'), ['harbour-notes.md'])
        stats = self.index.folder_stats()[str(self.folder)]
        self.assertEqual((stats['files'], stats['full'], stats['older']), (3, 3, 0))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_new_defaults(self):
        settings = config.load(self.home)
        self.assertEqual((settings['setup_done'], settings['rerank_enabled'], settings['answer_beyond_files']), (False, False, False))
        self.assertEqual((settings['folder_since'], settings['tracked_folders'], settings['model_download_asked']), ({}, [], False))
        self.assertIsNot(settings['folder_since'], config.DEFAULTS['folder_since'])

    def test_an_older_settings_file_keeps_its_values(self):
        stored = {'start_with_windows_configured': True, 'rerank_enabled': True, 'answer_beyond_files': True,
                  'model_download_asked': True, 'tracked_folders': [str(self.home)]}
        (self.home / 'settings.json').write_text(json.dumps(stored), encoding='utf-8')
        settings = config.load(self.home)
        self.assertEqual((settings['rerank_enabled'], settings['answer_beyond_files'], settings['model_download_asked']), (True, True, True))
        self.assertEqual((settings['setup_done'], settings['folder_since']), (False, {}))
        settings.save()
        self.assertTrue(json.loads((self.home / 'settings.json').read_text(encoding='utf-8'))['rerank_enabled'])

    def test_folder_since_helpers(self):
        settings = config.load(self.home)
        folder = self.home / 'Docs'
        folder.mkdir()
        self.assertIsNone(settings.since_for(folder))
        settings.set_since(str(folder), 1000)
        settings.set_since(folder, 2000)  # the same folder again: one entry
        self.assertEqual(settings['folder_since'], {str(folder): 2000.0})
        self.assertEqual(settings.since_for(str(folder) + os.sep), 2000.0)
        settings.save()
        self.assertEqual(config.load(self.home).since_for(folder), 2000.0)
        settings.clear_since(folder)
        self.assertEqual(settings['folder_since'], {})


if __name__ == '__main__':
    unittest.main()
