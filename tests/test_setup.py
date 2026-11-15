"""First-run setup (M12): the SETUP mode's three steps, the folder chooser, the add-folder sheet, start with Windows only
when chosen, and tracking a parent of tracked folders (Index.adopt).

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
Headless (QT_QPA_PLATFORM=offscreen), temp folders only: no server, no download, no Startup shortcut is written.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QThreadPool, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from alfred import config, extract, models, runtime  # noqa: E402
from alfred.index import Index, connect  # noqa: E402
from alfred.ui_kit import DRIVER  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def settle():
    QThreadPool.globalInstance().waitForDone(20000)
    for _ in range(5):
        _APP.processEvents()


def wait_for(condition, seconds=15):
    """Process events until ``condition()`` holds (an indexing run and its follow-up run finish)."""
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        settle()
        time.sleep(0.05)
    return condition()


class SetupBase(unittest.TestCase):
    needs = [models.ENGINE, 'embed-text']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.places = {}
        for name in ('Documents', 'Desktop', 'Downloads', 'Pictures'):
            folder = self.base / 'user' / name
            folder.mkdir(parents=True)
            (folder / f'{name.lower()}-notes.txt').write_text(f'{name} walrus ledger', encoding='utf-8')
            self.places[name] = folder
        (self.places['Documents'] / 'second.txt').write_text('another file', encoding='utf-8')
        from alfred import ai, startup, ui
        self.ui, self.startup = ui, startup
        self.started, self.startup_calls = [], []
        patches = [mock.patch.dict(os.environ, {'ALFRED_HOME': str(self.base / 'home'), 'USERPROFILE': str(self.base / 'user')}),
                   mock.patch.object(runtime, 'program_dir', lambda home=None: None),
                   mock.patch.object(runtime, 'PROGRAM_OVERRIDE', None),
                   mock.patch.object(ai.Embedder, 'from_settings', staticmethod(lambda settings: None)),
                   mock.patch.object(ui.ModelDownloads, 'start', lambda downloads, key: self.started.append(key)),
                   mock.patch.object(startup, 'enable', lambda folder=None: self.startup_calls.append('enable')),
                   mock.patch.object(startup, 'disable', lambda folder=None: self.startup_calls.append('disable')),
                   mock.patch.object(startup, 'enabled', lambda folder=None: 'enable' in self.startup_calls),
                   mock.patch.object(models, 'first_run_needs', lambda home=None: list(self.needs)),
                   mock.patch.object(ui, 'suggested_folders', lambda: [(n, self.places[n]) for n in ('Downloads', 'Pictures', 'Documents', 'Desktop')])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        home = self.base / 'home'
        home.mkdir()
        # No OCR in these runs (text files only; keeps the tests fast). The key marks the file as this Alfred's.
        (home / 'settings.json').write_text(json.dumps({'start_with_windows_configured': False, 'ocr_enabled': False}), encoding='utf-8')
        self.win = ui.AlfredWindow()
        self.view = self.win.setup_view

    def tearDown(self):
        self.win.watcher.stop()
        self.win.reindex_pending = False
        settle()
        time.sleep(0.5)  # a follow-up run's timer fires while the temp folders still exist
        settle()
        self.win.reindex_pending = False
        settle()
        self.win.index.close()
        DRIVER.springs.clear()  # no animation of this window may run after it is gone
        DRIVER.loops.clear()
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)  # gone now, not at the end of the whole run
        self.tmp.cleanup()

    def enter(self, widget=None):
        QApplication.sendEvent(widget or self.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
        settle()

    def rows(self):
        return [(box.text(), box.isChecked(), box.isEnabled()) for _, box, _ in self.view.rows]


class SetupFlowTests(SetupBase):
    def test_clean_home_opens_in_setup_with_documents_and_desktop_ticked(self):
        self.assertTrue(self.win.start_setup_if_needed())
        settle()
        self.assertEqual(self.win.mode, self.ui.SETUP)
        self.assertTrue(self.win.search.isHidden())
        self.assertTrue(self.win.modes.isHidden())
        self.assertEqual(self.win.setup_title.text(), 'What should Alfred search?')
        self.assertEqual(self.rows(), [('Documents', True, True), ('Desktop', True, True), ('Downloads', False, True), ('Pictures', False, True)])
        self.assertEqual([count.text() for _, _, count in self.view.rows], ['2 files', '1 file', '1 file', '1 file'])
        self.assertEqual(self.view.chooser.count, 3)
        self.assertEqual(self.view.primary.text(), 'Continue')
        for _, box, _ in self.view.rows:
            box.setChecked(False)
        self.assertEqual(self.view.primary.text(), 'Continue without folders')
        self.assertEqual(self.startup_calls, [])  # nothing is switched on before the user chose

    def test_enter_three_times_tracks_the_defaults_and_starts_the_downloads(self):
        self.win.start_setup_if_needed()
        settle()
        self.enter()
        self.assertEqual(self.view.step, 1)
        self.assertEqual(self.win.setup_title.text(), 'Search by meaning')
        self.assertEqual([Path(f) for f in self.win.settings['tracked_folders']], [self.places['Documents'], self.places['Desktop']])
        self.assertEqual(self.win.settings['folder_since'], {})
        total = sum(models.size_of(key) for key in self.needs)
        self.assertEqual(self.view.download_row.title.text(), f'Download the search model ({total / 1e6:,.0f} MB, once)')
        self.enter()
        self.assertEqual(self.started, self.needs)
        self.assertTrue(self.win.settings['model_download_asked'])
        self.assertEqual(self.view.step, 2)
        self.enter()
        self.assertEqual(self.win.mode, self.ui.SEARCH)
        self.assertTrue(self.win.settings['setup_done'])
        self.assertTrue(config.load(self.win.settings.home)['setup_done'])
        self.assertEqual(self.startup_calls, ['enable'])
        self.assertTrue(self.win.settings['start_with_windows_configured'])
        self.assertFalse(self.win.search.isHidden())
        self.assertFalse(self.win.start_setup_if_needed())  # never again by itself
        settle()
        self.assertEqual(self.win.index.status()['files'], 3)  # indexed in one run

    def test_only_new_files_for_the_ticked_folders(self):
        self.win.start_setup_if_needed()
        settle()
        self.view.chooser.only_new.setChecked(True)
        self.enter()
        settle()
        for name in ('Documents', 'Desktop'):
            self.assertIsNotNone(self.win.settings.since_for(self.places[name]))
        stats = self.win.index.folder_stats()[str(self.places['Documents'])]
        self.assertEqual((stats['older'], stats['full']), (2, 0))

    def test_back_does_not_track_twice_and_resumes_after_closing(self):
        self.win.start_setup_if_needed()
        settle()
        self.enter()
        with mock.patch.object(self.win, 'hide_panel') as hide:
            QApplication.sendEvent(self.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
            hide.assert_called_once()
            self.win.maybe_hide()  # losing focus does not hide setup
            hide.assert_called_once()
        self.assertEqual((self.win.mode, self.view.step), (self.ui.SETUP, 1))  # opening Alfred again resumes here
        self.view.back()
        self.assertEqual(self.view.step, 0)
        self.assertEqual(self.rows()[:2], [('Documents', True, False), ('Desktop', True, False)])
        toasts = []
        with mock.patch.object(self.win, 'toast', lambda text, *a, **k: toasts.append(text)):
            self.enter()
        self.assertEqual(len(self.win.settings['tracked_folders']), 2)
        self.assertEqual(toasts, [])

    def test_skip_setup(self):
        self.win.start_setup_if_needed()
        self.view.skip_button.click()
        settle()
        self.assertEqual(self.win.mode, self.ui.SEARCH)
        self.assertTrue(self.win.settings['setup_done'])
        self.assertEqual(self.win.settings['tracked_folders'], [])
        self.assertEqual(self.startup_calls, ['enable'])  # the default when setup is skipped
        self.assertFalse(self.win.start_setup_if_needed())

    def test_existing_folders_mark_setup_done_silently(self):
        self.win.settings['tracked_folders'] = [str(self.places['Documents'])]
        self.win.settings['start_with_windows_configured'] = True
        self.assertFalse(self.win.start_setup_if_needed())
        settle()
        self.assertTrue(self.win.settings['setup_done'])
        self.assertEqual(self.win.mode, self.ui.SEARCH)
        self.assertEqual(self.startup_calls, [])  # an existing user's choice is untouched

    def test_start_with_windows_off_and_download_off(self):
        self.win.start_setup_if_needed()
        settle()
        self.enter()
        self.view.download.setChecked(False)
        self.enter()
        self.assertEqual(self.started, [])
        self.assertTrue(self.win.settings['model_download_asked'])
        self.view.start_login.setChecked(False)
        self.enter()
        self.assertEqual(self.startup_calls, ['disable'])
        self.assertTrue(self.win.settings['start_with_windows_configured'])

    def test_the_shortcut_ticks_the_keycaps_on_step_three(self):
        self.win.active_hotkey = 'Ctrl+Alt+L'
        self.win.start_setup_if_needed()
        settle()
        self.view.go(2, 1)
        self.assertEqual(self.view.keycaps.keys, ['Ctrl', 'Alt', 'L'])
        with mock.patch.object(self.win, 'isVisible', return_value=True), mock.patch.object(self.win, 'toggle_panel') as toggle:
            self.win.hotkey_pressed()
            toggle.assert_not_called()
        self.assertTrue(self.view.keycaps.done)
        self.assertIn('That’s it', self.view.try_note.text())
        self.view.go(1, -1)
        with mock.patch.object(self.win, 'isVisible', return_value=True), mock.patch.object(self.win, 'toggle_panel') as toggle:
            self.win.hotkey_pressed()
            toggle.assert_called_once()

    def test_no_shortcut_shows_the_recorder(self):
        self.win.active_hotkey = ''
        self.win.start_setup_if_needed()
        self.view.go(2, 1)
        self.assertFalse(self.view.recorder.isHidden())
        self.assertIn('icon near the clock', self.view.try_note.text())


class SetupReadyTests(SetupBase):
    needs = []

    def test_search_by_meaning_ready(self):
        self.win.start_setup_if_needed()
        settle()
        self.enter()
        self.assertFalse(self.view.ready.isHidden())
        self.assertTrue(self.view.download_card.isHidden())
        self.enter()
        self.assertEqual(self.started, [])

    def test_run_setup_again_shows_tracked_folders(self):
        self.win.settings['tracked_folders'] = [str(self.places['Pictures'])]
        self.win.settings['setup_done'] = True
        self.win.run_setup()
        settle()
        self.assertIn(('Pictures', True, False), self.rows())
        self.assertEqual(self.win.mode, self.ui.SETUP)


class ChooserTests(SetupBase):
    def test_defaults_and_thresholds(self):
        chooser = self.ui.FolderChooser(self.win)
        self.assertTrue(chooser.everything.isChecked())
        self.assertFalse(chooser.only_new_files())
        self.assertEqual(chooser.everything.text(), 'Everything in it')
        self.assertEqual(chooser.everything_note.text(), 'All files become searchable.')
        chooser.set_count(self.ui.ESTIMATE_ABOVE)
        self.assertNotIn('About', chooser.everything_note.text())
        chooser.set_count(12480)
        self.assertEqual(chooser.everything_note.text(), 'All 12,480 files become searchable. About 10 minutes.')
        self.assertTrue(chooser.hint.isHidden())
        chooser.set_count(self.ui.LARGE_FOLDER + 1)
        self.assertFalse(chooser.hint.isHidden())
        self.assertEqual(self.ui.FolderChooser(self.win, plural=True).everything.text(), 'Everything in them')
        self.assertEqual(self.ui.estimate_text(300000), 'About 4 hours')


class SheetTests(SetupBase):
    def test_add_folder_sheet_tracks_with_the_choice(self):
        folder = self.places['Downloads']
        self.assertTrue(self.win.offer_folder(folder))
        settle()
        sheet = self.win.sheet
        self.assertTrue(self.win.sheet_open)
        self.assertTrue(self.win.wants_expanded())
        self.assertEqual((sheet.name.text(), sheet.count.text(), sheet.chooser.count), ('Downloads', '1 file', 1))
        self.assertEqual(self.win.settings['tracked_folders'], [])  # nothing is tracked before "Track folder"
        sheet.chooser.only_new.setChecked(True)
        self.enter(sheet)
        self.assertFalse(self.win.sheet_open)
        self.assertEqual([Path(f) for f in self.win.settings['tracked_folders']], [folder])
        self.assertIsNotNone(self.win.settings.since_for(folder))

    def test_escape_cancels(self):
        self.win.offer_folder(self.places['Pictures'])
        QApplication.sendEvent(self.win.sheet, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertFalse(self.win.sheet_open)
        self.assertEqual(self.win.settings['tracked_folders'], [])

    def test_settings_rows_show_only_new_folders(self):
        folder = self.places['Documents']
        self.win.track_folder(folder, only_new=True)
        settle()
        self.win.set_mode(self.ui.SETTINGS, animate=False)
        self.win.refresh_status()
        [row] = self.win.settings_view.tracked.rows
        self.assertTrue(row.includes.text().startswith('New files since '))
        self.assertFalse(row.include_older.isHidden())
        self.assertIn('2 files · 0 fully searchable', row.status.text())
        row.include_older.click()
        settle()
        self.assertIsNone(self.win.settings.since_for(folder))
        self.assertEqual(row.includes.text(), 'Everything')
        self.assertEqual(self.win.index.folder_stats()[str(folder)]['older'], 0)
        found = lambda: [r['filename'] for r in self.win.index.search('walrus ledger', mode='keyword')]
        self.assertTrue(wait_for(lambda: found() == ['documents-notes.txt']), found())


class AdoptTests(SetupBase):
    def test_a_parent_keeps_the_inner_folders_files(self):
        inner = self.places['Documents']
        self.win.track_folder(inner)
        settle()
        self.win.index.update()  # a full run: its documents are read with the current headers (no re-read later)
        parent = self.base / 'user'
        with mock.patch.object(extract, 'extract', wraps=extract.extract) as read:
            with mock.patch.object(self.ui.paths, 'forbidden_reason', lambda folder, home=None: None):  # the user folder is a test stand-in
                self.assertTrue(self.win.track_folder(parent, only_new=True))
            settle()
            read_names = sorted(Path(call.args[0]).name for call in read.call_args_list)
        self.assertEqual([Path(f) for f in self.win.settings['tracked_folders']], [parent])
        self.assertNotIn('documents-notes.txt', read_names)  # moved, not read again
        con = connect(self.win.index.db_path)
        try:
            rows = dict(con.execute('SELECT path, status FROM files'))
            roots = {root for (root,) in con.execute('SELECT DISTINCT root FROM files')}
        finally:
            con.close()
        self.assertEqual(roots, {str(parent)})
        self.assertEqual(rows[str(inner / 'documents-notes.txt')], 'ok')
        self.assertEqual(rows[str(self.places['Desktop'] / 'desktop-notes.txt')], 'skipped')
        self.assertEqual([r['filename'] for r in self.win.index.search('Documents walrus', mode='keyword')], ['documents-notes.txt'])


class AdoptIndexTests(unittest.TestCase):
    def test_adopt_moves_rows_and_includes_older(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(os.path.realpath(tmp))
            inner = base / 'Parent' / 'Inner'
            inner.mkdir(parents=True)
            (inner / 'a.txt').write_text('pelican roster', encoding='utf-8')
            settings = config.load(base / 'home')
            settings['tracked_folders'] = [str(inner)]
            index = Index(settings)
            try:
                index.track_only_new(inner)
                index.update()
                settings['tracked_folders'] = [str(base / 'Parent')]
                settings.clear_since(inner)
                self.assertEqual(index.adopt(base / 'Parent', [inner], include_older=True), 1)
                index.update()
                self.assertEqual([r['filename'] for r in index.search('pelican roster', mode='keyword')], ['a.txt'])
                self.assertEqual(index.folder_stats()[str(base / 'Parent')]['full'], 1)
            finally:
                index.close()


if __name__ == '__main__':
    unittest.main()
