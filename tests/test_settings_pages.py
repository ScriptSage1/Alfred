"""Settings as six pages (M12, design 3.1 and 3.2): the pages and their order, the remembered page, show_page, the keys
that round-trip through load → change → save, the two new rows, no banned words, Advanced's closed sections.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
Headless (QT_QPA_PLATFORM=offscreen), temp folders only: no server, no download, no Startup shortcut is written.
"""
import json
import os
import re
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
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit, QWidget  # noqa: E402

from alfred import config, models, runtime  # noqa: E402
from alfred.ui_kit import DRIVER  # noqa: E402

_APP = QApplication.instance() or QApplication([])
BANNED = re.compile(r'(?i:embeddings|inference|passages|nvidia-smi|llama\.cpp|ALFRED_MODELS_DIR|picture part)|\bGPU')


def settle():
    QThreadPool.globalInstance().waitForDone(20000)
    for _ in range(5):
        _APP.processEvents()


class SettingsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(os.path.realpath(self.tmp.name))
        from alfred import ai, startup, ui
        self.ui = ui
        patches = [mock.patch.dict(os.environ, {'ALFRED_HOME': str(self.base / 'home'), 'USERPROFILE': str(self.base / 'user'),
                                                'OLLAMA_MODELS': str(self.base / 'ollama')}),
                   mock.patch.object(runtime, 'program_dir', lambda home=None: None),
                   mock.patch.object(runtime, 'PROGRAM_OVERRIDE', None),
                   mock.patch.object(ai.Embedder, 'from_settings', staticmethod(lambda settings: None)),
                   mock.patch.object(ui.ModelDownloads, 'start', lambda downloads, key: None),
                   mock.patch.object(startup, 'enable', lambda folder=None: None),
                   mock.patch.object(startup, 'disable', lambda folder=None: None),
                   mock.patch.object(startup, 'enabled', lambda folder=None: False),
                   mock.patch.object(ui, 'suggested_folders', lambda: [])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        home = self.base / 'home'
        home.mkdir()
        (home / 'settings.json').write_text(json.dumps({'start_with_windows_configured': True, 'setup_done': True, 'ocr_enabled': False}),
                                            encoding='utf-8')
        self.win = ui.AlfredWindow()
        self.view = self.win.settings_view

    def tearDown(self):
        settle()
        self.win.watcher.stop()
        self.win.index.close()
        DRIVER.springs.clear()
        DRIVER.loops.clear()
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.tmp.cleanup()

    def open(self):
        self.win.set_mode(self.ui.SETTINGS, animate=False)
        settle()

    def key(self, key, widget=None):
        QApplication.sendEvent(widget or self.win.search, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        settle()


class PageTests(SettingsBase):
    def test_six_pages_in_order(self):
        self.assertEqual(self.ui.SETTINGS_PAGES, ('General', 'Folders', 'Search', 'Ask Alfred', 'AI tools', 'Advanced'))
        self.assertEqual([self.view.stack.widget(i).name for i in range(self.view.stack.count())], list(self.ui.SETTINGS_PAGES))
        self.assertEqual(self.view.pages_list.names, list(self.ui.SETTINGS_PAGES))

    def test_opens_on_general_and_remembers_the_last_page(self):
        self.open()
        self.assertEqual(self.view.current_page(), 'General')
        self.assertIs(self.view.stack.currentWidget(), self.view.pages['General'])
        self.view.show_page('Search')
        self.win.set_mode(self.ui.SEARCH, animate=False)
        self.open()
        self.assertEqual(self.view.current_page(), 'Search')
        self.assertIs(self.view.stack.currentWidget(), self.view.pages['Search'])

    def test_show_page_and_its_callers(self):
        self.win.open_settings('Advanced', 'API keys')
        settle()
        self.assertEqual(self.win.mode, self.ui.SETTINGS)
        self.assertEqual(self.view.current_page(), 'Advanced')
        self.assertTrue(self.view.sections['API keys'].open)
        self.win.ask_view.picked({'action': 'settings'})  # the model picker's "Accounts and models…"
        settle()
        self.assertEqual(self.view.current_page(), 'Ask Alfred')
        self.win.open_settings()  # the tray: the last page
        self.assertEqual(self.view.current_page(), 'Ask Alfred')
        with self.assertRaises(ValueError):
            self.view.show_page('Nowhere')

    def test_up_and_down_move_between_pages_and_tab_enters_the_page(self):
        self.open()
        self.key(Qt.Key_Down)
        self.key(Qt.Key_Down)
        self.assertEqual(self.view.current_page(), 'Search')
        self.key(Qt.Key_Up)
        self.assertEqual(self.view.current_page(), 'Folders')
        self.key(Qt.Key_Up)
        self.key(Qt.Key_Up)  # stays on the first page
        self.assertEqual(self.view.current_page(), 'General')
        self.key(Qt.Key_End, self.view.pages_list)
        self.assertEqual(self.view.current_page(), 'Advanced')
        self.key(Qt.Key_Up, self.view.pages_list)
        self.assertEqual(self.view.current_page(), 'AI tools')
        self.assertEqual(self.win.mode, self.ui.SETTINGS)  # Tab no longer switches to Ask Alfred in Settings
        self.view.show_page('General')
        self.assertTrue(self.view.enter_page())
        with mock.patch.object(self.win, 'set_mode') as set_mode:
            self.key(Qt.Key_Escape, self.view.pages_list)  # Esc from the list goes back, as from the search box
            set_mode.assert_called_once_with(self.ui.SEARCH)

    def test_advanced_sections_start_closed_and_open(self):
        sections = self.view.sections
        self.assertEqual(list(sections), ['Performance', 'Model files', 'Answer quality', 'API keys', 'Diagnostics'])
        for section in sections.values():
            self.assertFalse(section.open)
            self.assertEqual(section.body.maximumHeight(), 0)
        sections['Performance'].set_open(True, animate=False)
        self.assertGreater(sections['Performance'].body.maximumHeight(), 1000)
        sections['Performance'].set_open(False, animate=False)
        self.assertEqual(sections['Performance'].body.maximumHeight(), 0)

    def test_a_page_that_scrolls_fades_at_its_edges(self):
        self.open()
        self.view.show_page('Advanced')
        for section in self.view.sections.values():
            section.set_open(True, animate=False)
        hide = mock.patch.object(self.win, 'maybe_hide', lambda: None)  # offscreen no window is active
        hide.start()
        self.addCleanup(hide.stop)
        self.win.show()
        settle()
        page = self.view.pages['Advanced']
        try:
            self.assertTrue(page.scrolls())
            self.assertIsNotNone(page.mask)  # at the top: the bottom edge fades
            for section in self.view.sections.values():
                section.set_open(False, animate=False)
            settle()
            self.assertFalse(page.scrolls())
            self.assertIsNone(page.mask)
        finally:
            self.win.hide()

    def test_no_banned_words_in_settings(self):
        self.open()
        self.view.show_index_info()
        self.view.show_gpu_check()
        settle()
        texts = []
        for widget in [self.view] + self.view.findChildren(QWidget):
            texts.append(widget.toolTip())
            if isinstance(widget, QLabel):
                texts.append(widget.text())
            elif isinstance(widget, QAbstractButton):
                texts.append(widget.text())
            elif isinstance(widget, QLineEdit):
                texts.append(widget.placeholderText())
            elif isinstance(widget, QComboBox):
                texts += [widget.itemText(i) for i in range(widget.count())]
                texts += [str(widget.itemData(i, Qt.ToolTipRole) or '') for i in range(widget.count())]
        texts += self.view.pages_list.names + list(self.view.sections)
        found = [text for text in texts if text and BANNED.search(text)]
        self.assertEqual(found, [])
        self.assertTrue(any('graphics card' in text for text in texts))
        # Text from elsewhere is put in Settings' words.
        self.assertEqual(self.ui.plain('The search engine (llama.cpp) is not installed'), 'The search engine is not installed')
        self.assertEqual(self.ui.plain('The picture part of the search model is not downloaded'), 'The picture search model is not downloaded')
        self.assertIsNone(BANNED.search(self.ui.plain('GPU ACTIVE · 3 embeddings · 2 passages via nvidia-smi')))


class RoundTripTests(SettingsBase):
    """Every key that had a control before the pages still loads into it and saves from it; so do the two new rows."""

    def set_combo(self, combo, data=None, index=None):
        combo.setCurrentIndex(combo.findData(data) if index is None else index)

    def test_every_key_round_trips(self):
        from alfred import rerank
        v = self.view
        other_model = [name for name in rerank.MODELS if name != rerank.DEFAULT][0]
        cases = [  # key, value loaded first, change made in the control, value saved
            ('theme', 'system', lambda: v.theme.setCurrentIndex(2), 'light'),
            ('see_through', True, lambda: v.see_through.setChecked(False), False),
            ('reduce_motion', False, lambda: v.reduce_motion.setChecked(True), True),
            ('auto_index', True, lambda: v.auto_index.setChecked(False), False),
            ('index_on_battery', False, lambda: v.index_on_battery.setChecked(True), True),
            ('embed_enabled', False, lambda: v.embed_enabled.setChecked(True), True),
            ('ocr_enabled', True, lambda: v.ocr_enabled.setChecked(False), False),
            ('picture_search', True, lambda: v.picture_search.setChecked(False), False),
            ('answer_beyond_files', False, lambda: v.beyond.setChecked(True), True),
            ('save_chat_history', True, lambda: v.save_chats.setChecked(False), False),
            ('device', 'auto', lambda: v.device.setCurrentIndex(1), 'cpu'),
            ('embed_device', 'auto', lambda: v.embed_device.setCurrentIndex(1), 'cpu'),
            ('search_on_gpu', False, lambda: v.search_on_gpu.setChecked(True), True),
            ('keep_alive_minutes', 5, lambda: self.set_combo(v.keep_minutes, 60), 60),
            ('rerank_enabled', False, lambda: v.rerank_enabled.setChecked(True), True),
            ('rerank_model', rerank.DEFAULT, lambda: self.set_combo(v.rerank_model, other_model), other_model),
            ('video_interval', 10, lambda: v.video_interval.setValue(30), 30),
            ('compatible_base_url', '', lambda: v.compatible_url.setText('http://localhost:1234/v1'), 'http://localhost:1234/v1'),
        ]
        settings = self.win.settings
        with mock.patch.object(self.win, 'start_index'), mock.patch.object(runtime, 'stop'):
            for key, first, change, saved in cases:
                with self.subTest(key=key):
                    settings[key] = first
                    settings.save()
                    self.open()
                    change()
                    v.save()
                    self.assertEqual(settings[key], saved)
                    self.assertEqual(config.load(settings.home)[key], saved)
                    self.open()  # loads back into the control: saving again keeps it
                    v.save()
                    self.assertEqual(config.load(settings.home)[key], saved)

    def test_thinking_round_trips(self):
        self.open()
        self.ui.fill_effort(self.view.effort, None, [('low', 'Low'), ('high', 'High')], 'How long it thinks', 'low')
        self.view.effort.setCurrentIndex(1)
        self.view.save()
        self.assertEqual(config.load(self.win.settings.home)['effort'], 'high')

    def test_picture_search_and_ocr_update_the_index(self):
        self.open()
        for toggle in (self.view.picture_search, self.view.ocr_enabled):
            with self.subTest(toggle=toggle), mock.patch.object(self.win, 'start_index') as start:
                toggle.setChecked(not toggle.isChecked())
                self.view.save()
                start.assert_called_once_with(quiet=True)
        with mock.patch.object(self.win, 'start_index') as start:
            self.view.index_on_battery.setChecked(not self.view.index_on_battery.isChecked())
            self.view.save()
            start.assert_not_called()

    def test_picture_search_says_why_it_is_off(self):
        self.open()
        self.view.embed_enabled.setChecked(False)
        self.assertFalse(self.view.picture_search.isEnabled())
        self.assertIn('search by meaning', self.view.picture_row.caption.text())
        self.view.embed_enabled.setChecked(True)
        self.assertTrue(self.view.picture_search.isEnabled())

    def test_search_by_meaning_row_offers_its_download(self):
        started = []
        with mock.patch.object(self.ui.ModelDownloads, 'start', lambda downloads, key: started.append(key)), \
                mock.patch.object(self.ui, 'ask_box', lambda *a, **k: self.ui.QMessageBox.Ok):
            self.open()
            settle()
            self.assertIn('not downloaded', self.view.meaning_row.caption.text())
            self.assertFalse(self.view.meaning_button.isHidden())
            self.view.meaning_button.click()
        self.assertEqual(sorted(started), sorted(models.FIRST_RUN))


if __name__ == '__main__':
    unittest.main()
