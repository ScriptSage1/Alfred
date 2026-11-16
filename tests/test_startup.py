"""Start with Windows: the Startup shortcut is written and read in Alfred's own process (alfred.shortcut), never through
PowerShell or any other program, and a normal start leaves an up-to-date shortcut alone.

Run from the repository folder:  .venv\\Scripts\\python -m unittest tests.test_startup -v
Temp folders only: the real Startup folder is never touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import shortcut, startup  # noqa: E402


@unittest.skipUnless(os.name == 'nt', 'Windows shortcuts')
class StartupShortcutTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='alfred-startup-'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        # Any program started during a test fails it: the point is that none is.
        patcher = mock.patch('subprocess.Popen', side_effect=AssertionError('a program was started'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shortcut_round_trip(self):
        link = self.folder / 'Test.lnk'
        shortcut.write(link, sys.executable, '-m alfred "a b"', self.folder, startup.ICON, 0, shortcut.SW_SHOWMINNOACTIVE, 'Test')
        found = shortcut.read(link)
        self.assertEqual(os.path.normcase(found['target']), os.path.normcase(sys.executable))
        self.assertEqual(found['arguments'], '-m alfred "a b"')
        self.assertEqual(os.path.normcase(found['workdir']), os.path.normcase(str(self.folder)))
        self.assertEqual(os.path.normcase(found['icon']), os.path.normcase(str(startup.ICON)))
        self.assertEqual((found['show'], found['description']), (shortcut.SW_SHOWMINNOACTIVE, 'Test'))

    def test_enable_disable(self):
        self.assertFalse(startup.enabled(self.folder))
        startup.enable(self.folder)
        self.assertTrue(startup.enabled(self.folder))
        startup.disable(self.folder)
        self.assertFalse(startup.enabled(self.folder))

    def test_refresh_leaves_a_current_shortcut_alone(self):
        self.assertFalse(startup.refresh(self.folder))  # none there: nothing is written
        self.assertFalse((self.folder / startup.NAME).exists())
        startup.enable(self.folder)
        with mock.patch.object(startup, 'enable') as enable:
            self.assertFalse(startup.refresh(self.folder))
        enable.assert_not_called()

    def test_refresh_updates_a_stale_icon(self):
        target, arguments = startup.command()
        shortcut.write(self.folder / startup.NAME, target, arguments, startup.REPO, sys.executable)
        self.assertTrue(startup.refresh(self.folder))
        self.assertEqual(os.path.normcase(shortcut.read(self.folder / startup.NAME)['icon']), os.path.normcase(str(startup.ICON)))

    def test_refresh_ignores_another_programs_shortcut(self):
        shortcut.write(self.folder / startup.NAME, sys.executable, '', self.folder, sys.executable)
        self.assertFalse(startup.refresh(self.folder))


if __name__ == '__main__':
    unittest.main()
