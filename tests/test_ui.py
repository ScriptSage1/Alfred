"""UI smoke tests: the main window builds and searches run, headless (QT_QPA_PLATFORM=offscreen, temp folders only)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class SearchViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(os.path.realpath(self.tmp.name))
        self.folder = base / 'Tracked'
        self.folder.mkdir()
        self.patches = [mock.patch.dict(os.environ, {'ALFRED_HOME': str(base / 'home')})]
        for patch in self.patches:
            patch.start()
        from alfred import ai, ui
        self.patches.append(mock.patch.object(ai.Embedder, 'from_settings', staticmethod(lambda settings: None)))
        self.patches[-1].start()
        (self.folder / 'harbour.txt').write_text('harbour pilot schedule for the northern quay', encoding='utf-8')
        (self.folder / 'partial.txt').write_text('the pilot arrived late', encoding='utf-8')
        self.win = ui.AlfredWindow()
        self.win.settings['tracked_folders'] = [str(self.folder)]
        self.win.index.update()
        self.view = self.win.search_view

    def tearDown(self):
        QThreadPool.globalInstance().waitForDone(10000)  # a search still running must not meet a closed index
        self.win.index.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.win.deleteLater()
        self.tmp.cleanup()

    def search(self, text):
        self.view.query = text
        self.view.run()
        deadline = time.monotonic() + 15
        while self.view.shown != text and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.view.shown, text)

    def titles(self):
        return [value for kind, value in self.view.list.items if kind == 'header'] + \
               [value.get('title') for kind, value in self.view.list.items if kind == 'row']

    def test_settings_lists_ai_tools_and_their_calls(self):
        from alfred import mcp_server, ui
        home, view = self.win.settings.home, self.win.settings_view
        mcp_server.set_allowed(home, 'some-tool', True)
        mcp_server.log_call(home, {'time': '2026-01-01 10:00:00', 'client': 'some-tool', 'tool': 'search_files', 'query': 'tides',
                                   'count': 2, 'outcome': 'ok'})
        view.show_other_tools()
        holder = view.mcp_clients.parentWidget()
        self.assertIn('-m alfred --mcp', view.mcp_command.text())
        self.assertIn('some-tool', [w.text() for w in holder.findChildren(ui.QLabel)])
        self.assertIn('tides', view.mcp_calls.text())
        [remove] = [b for b in holder.findChildren(ui.QPushButton) if b.text() == 'Remove']
        remove.click()
        self.assertEqual(mcp_server.list_clients(home), [])

    def test_window_builds_and_search_lists_results(self):
        self.search('harbour pilot')
        self.assertIn('harbour.txt', self.titles())

    def test_weaker_matches_are_offered_not_hidden(self):
        self.search('quay zebra giraffe')  # one word of three: found, but not confidently
        self.assertEqual(self.view.empty.title.text(), 'No strong matches for “quay zebra giraffe”')
        self.assertEqual(self.view.stack.currentIndex(), 0)
        self.view.show_weak()
        self.assertEqual(self.view.stack.currentIndex(), 1)
        self.assertIn('Weaker matches', self.titles())
        self.assertIn('harbour.txt', self.titles())

    def test_weaker_link_row_under_confident_results(self):
        self.search('pilot')  # both files are confident; no weak row needed
        self.assertFalse(any(str(t).startswith('Show ') for t in self.titles()))

    def test_disk_walk_is_not_repeated_per_keystroke(self):
        with mock.patch.object(self.win.index, 'pending_changes', wraps=self.win.index.pending_changes) as walk:
            self.search('zzzqqq')
            self.search('zzzqqqx')
            self.assertEqual(walk.call_count, 1)

    def test_filters_narrow_a_search_and_clear(self):
        from alfred import ui
        (self.folder / 'deck.md').write_text('harbour pilot notes in markdown', encoding='utf-8')
        os.utime(self.folder / 'harbour.txt', (time.time() - 400 * 86400,) * 2)
        self.win.index.update()
        self.view.set_query('harbour pilot')  # what typing does: shows the filter chips and schedules the search
        self.assertFalse(self.view.filters.isHidden())
        self.search('harbour pilot')
        self.view.kind_chips['pdfs'].setChecked(True)  # toggling a chip re-runs the search
        self.wait_for_empty('No results for')
        self.assertIn('Clear filters', [b.text() for b in self.view.empty.findChildren(ui.QPushButton)])
        self.view.clear_filters()
        self.wait_for_titles('harbour.txt')
        self.view.cycle_age()  # Past week: the old file drops out, the fresh one stays
        self.assertEqual(self.view.age_chip.text(), 'Past week')
        self.wait_for(lambda: 'deck.md' in self.titles() and 'harbour.txt' not in self.titles())

    def wait_for(self, check):
        deadline = time.monotonic() + 15
        while not check() and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        self.assertTrue(check())

    def wait_for_titles(self, name):
        self.wait_for(lambda: name in self.titles() and self.view.stack.currentIndex() == 1)

    def wait_for_empty(self, text):
        self.wait_for(lambda: self.view.stack.currentIndex() == 0 and text in self.view.empty.title.text())


if __name__ == '__main__':
    unittest.main()
