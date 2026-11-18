"""The build self-test (python -m juno --selftest): it passes from the source tree, stays inside its own temporary
folder and leaves JUNO_HOME alone. No window, no llama.cpp server (it only looks for the engine), no download.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from juno import selftest  # noqa: E402


class SelfTestTests(unittest.TestCase):
    def test_arguments(self):
        self.assertEqual(selftest._arguments(['--selftest', 'D:/work', '--selftest-log', 'D:/r.log']), ('D:/work', 'D:/r.log'))
        self.assertEqual(selftest._arguments(['--selftest-log', 'D:/r.log', '--selftest']), (None, 'D:/r.log'))
        folder, log = selftest._arguments(['--selftest'])
        self.assertIsNone(folder)
        self.assertTrue(log.endswith('juno-selftest.log'))

    def test_selftest_passes_and_stays_in_its_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, work, log = tmp / 'home', tmp / 'work', tmp / 'selftest.log'
            home.mkdir()
            env = dict(os.environ, JUNO_HOME=str(home), QT_QPA_PLATFORM='offscreen')
            out = subprocess.run([sys.executable, '-m', 'juno', '--selftest', str(work), '--selftest-log', str(log)],
                                 cwd=str(REPO), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                 timeout=600, stdin=subprocess.DEVNULL)
            text = log.read_text(encoding='utf-8')
            self.assertEqual(out.returncode, 0, text + out.stderr[-2000:])
            self.assertEqual(text.strip().splitlines()[-1], 'SELFTEST OK')
            self.assertIn('OCR of a picture', text)
            self.assertEqual(list(home.iterdir()), [])  # the real data folder is never touched
            self.assertEqual(list(work.iterdir()), [])  # its temporary folder is removed at the end


if __name__ == '__main__':
    unittest.main()
