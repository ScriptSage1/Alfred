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


if __name__ == '__main__':
    unittest.main()
