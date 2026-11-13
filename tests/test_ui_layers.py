"""Layered-indexing UI: progress text, the partial-index notice, background runs and battery resume.
Pure helpers are tested directly; window methods run against fakes or a headless window (no network, no model server)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _scratch, mock.patch.dict(os.environ, {'ALFRED_HOME': _scratch}):
    from alfred import ui  # noqa: E402

COMPLETE = {'names': [10, 10], 'text': [10, 10], 'meaning': [20, 20], 'pictures': [3, 3]}


class LayerTextTests(unittest.TestCase):
    def test_complete_and_zero_totals_show_ticks(self):
        self.assertEqual(ui.layers_text(COMPLETE), 'names ✓ · text ✓ · meaning ✓ · pictures ✓')
        self.assertEqual(ui.layers_text({'names': [5, 5], 'text': [5, 5], 'meaning': [0, 0], 'pictures': [0, 0]}),
                         'names ✓ · text ✓ · meaning ✓ · pictures ✓')

    def test_partial_layers_show_floored_percent(self):
        layers = {'names': [10, 10], 'text': [10, 10], 'meaning': [62, 100], 'pictures': [1, 10]}
        self.assertEqual(ui.layers_text(layers), 'names ✓ · text ✓ · meaning 62% · pictures 10%')

    def test_almost_done_never_shows_100(self):
        layers = dict(COMPLETE, meaning=[999, 1000])
        text = ui.layers_text(layers)
        self.assertIn('meaning 99%', text)
        self.assertNotIn('100', text)
        self.assertTrue(ui.layers_incomplete(layers))

    def test_status_line_lists_unfinished_layers_only(self):
        layers = {'names': [10, 10], 'text': [10, 10], 'meaning': [62, 100], 'pictures': [1, 10]}
        self.assertEqual(ui.layers_text(layers, unfinished_only=True), 'meaning 62% · pictures 10%')
        self.assertEqual(ui.layers_text(COMPLETE, unfinished_only=True), '')
        self.assertEqual(ui.layers_text(None, unfinished_only=True), '')

    def test_unknown_layers(self):
        self.assertEqual(ui.layers_text(None), '')
        self.assertEqual(ui.layers_notice(None), '')


class NoticeTests(unittest.TestCase):
    def test_names_first_incomplete_layer(self):
        layers = {'names': [10, 10], 'text': [4, 10], 'meaning': [0, 20], 'pictures': [1, 10]}
        self.assertEqual(ui.layers_notice(layers), 'Still indexing — text 40%. Results may be incomplete.')
        layers['text'] = [10, 10]
        self.assertEqual(ui.layers_notice(layers), 'Still indexing — meaning 0%. Results may be incomplete.')
        layers['meaning'] = [20, 20]
        self.assertEqual(ui.layers_notice(layers), 'Still indexing — pictures 10%. Results may be incomplete.')

    def test_empty_when_complete(self):
        self.assertEqual(ui.layers_notice(COMPLETE), '')

    def test_picture_rows_have_no_dangling_separator(self):
        self.assertEqual(ui.result_subtitle({'location': 'picture at 0:42', 'snippet': ''}), 'picture at 0:42')
        self.assertEqual(ui.result_subtitle({'location': 'page 3', 'snippet': 'harbour'}), 'page 3 — harbour')

    def test_phase_labels(self):
        self.assertEqual(ui.phase_label('names'), 'Listing files')
        self.assertEqual(ui.phase_label('reading pictures'), 'Reading pictures')
        self.assertEqual(ui.phase_label('embedding pictures'), 'Learning pictures')
        self.assertEqual(ui.phase_label('adding context'), 'Adding context')


class FakeWindow:
    """Just the attributes the window's small methods touch."""
    def __init__(self, visible=False):
        self.visible, self.indexing, self.index_background, self.priority_low = visible, False, False, False
        self.paused, self.started = None, []
        self.battery_timer = mock.Mock()
        self.index = mock.Mock()

    def isVisible(self):
        return self.visible

    def start_index(self, **kwargs):
        self.started.append(kwargs)

    sync_priority = ui.AlfredWindow.sync_priority
    battery_tick = ui.AlfredWindow.battery_tick


class PriorityTests(unittest.TestCase):
    def test_low_priority_only_for_hidden_automatic_runs(self):
        win = FakeWindow(visible=False)
        with mock.patch.object(ui.power, 'background_priority') as priority:
            win.indexing, win.index_background = True, True
            win.sync_priority()
            priority.assert_called_once_with(True)
            win.sync_priority()  # no change: no second call
            self.assertEqual(priority.call_count, 1)
            win.visible = True  # window shown
            win.sync_priority()
            priority.assert_called_with(False)
            win.visible = False  # hidden again, run still going
            win.sync_priority()
            priority.assert_called_with(True)
            win.indexing = False  # run ended
            win.sync_priority()
            priority.assert_called_with(False)
            self.assertEqual(priority.call_count, 4)

    def test_foreground_runs_never_lower_priority(self):
        win = FakeWindow(visible=False)
        win.indexing, win.index_background = True, False
        with mock.patch.object(ui.power, 'background_priority') as priority:
            win.sync_priority()
            priority.assert_not_called()


class BatteryTests(unittest.TestCase):
    def test_tick_waits_while_on_battery(self):
        win = FakeWindow()
        win.paused = 'battery'
        with mock.patch.object(ui.power, 'on_battery', return_value=True):
            win.battery_tick()
        self.assertEqual(win.started, [])
        win.battery_timer.stop.assert_not_called()

    def test_tick_starts_background_run_on_mains(self):
        win = FakeWindow()
        win.paused = 'battery'
        with mock.patch.object(ui.power, 'on_battery', return_value=False):
            win.battery_tick()
        win.battery_timer.stop.assert_called_once()
        self.assertEqual(win.started, [{'quiet': True, 'auto': True}])
        self.assertIsNone(win.paused)


class ForegroundBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(os.path.realpath(self.tmp.name))
        (base / 'Tracked').mkdir()
        self.patch = mock.patch.dict(os.environ, {'ALFRED_HOME': str(base / 'home')})
        self.patch.start()
        self.win = ui.AlfredWindow()
        self.win.settings['tracked_folders'] = [str(base / 'Tracked')]

    def tearDown(self):
        self.win.index.close()
        self.patch.stop()
        self.win.deleteLater()
        self.tmp.cleanup()

    def start(self, **kwargs):
        """Start a run with run_job captured; execute its work function against a fake Index.update."""
        jobs = []
        summary = {'indexed': 0, 'removed': 0, 'errors': 0, 'unchanged': 0, 'warnings': [], 'paused': kwargs.pop('paused', None)}
        self.win.index.update = mock.Mock(return_value=summary)
        with mock.patch.object(ui, 'run_job', lambda fn, done=None, failed=None, progress=None, **k: jobs.append((fn, done, failed))), \
                mock.patch.object(ui.ai.Embedder, 'from_settings', staticmethod(lambda s: None)), \
                mock.patch.object(ui.power, 'background_priority') as priority:
            self.win.start_index(**kwargs)
            fn, done, failed = jobs[0]
            hidden_low = priority.call_args_list[:]
            fn(mock.Mock())
            done(summary)
        return self.win.index.update.call_args.kwargs, hidden_low, priority.call_args_list

    def test_automatic_run_is_background_and_lowers_priority_while_hidden(self):
        kwargs, during, overall = self.start(quiet=True, auto=True)
        self.assertTrue(kwargs['background'])
        self.assertEqual([c.args for c in during], [(True,)])
        self.assertEqual([c.args for c in overall], [(True,), (False,)])

    def test_user_run_is_foreground_and_keeps_priority(self):
        kwargs, during, overall = self.start()
        self.assertFalse(kwargs['background'])
        self.assertEqual(overall, [])

    def test_startup_run_is_background_without_auto_toasts(self):
        kwargs, _, _ = self.start(quiet=True, background=True)
        self.assertTrue(kwargs['background'])

    def test_paused_on_battery_starts_timer_and_shows_status(self):
        self.start(quiet=True, auto=True, paused='battery')
        self.assertTrue(self.win.battery_timer.isActive())
        self.assertEqual(self.win.paused, 'battery')
        self.win.status = dict(self.win.status, files=5, folders=1)
        self.assertIn('Paused on battery', self.win.status_text())
        # any other run stops the timer
        self.start(quiet=True)
        self.assertFalse(self.win.battery_timer.isActive())


if __name__ == '__main__':
    unittest.main()
