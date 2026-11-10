"""Open-at-a-spot: the command lines (nothing is launched) and the plain-open fallback."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import opener  # noqa: E402

PDF = r'C:\Users\Me\Docs\Annual report #2.pdf'
VIDEO = r'C:\Users\Me\Videos\Trip 2024.mp4'


class CommandTests(unittest.TestCase):
    def test_browsers_get_a_page_fragment_on_an_escaped_uri(self):
        for exe in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', r'C:\x\Chrome.exe', r'C:\x\firefox.exe', r'C:\x\brave.exe'):
            command = opener.command_for(exe, PDF, {'page': 7})
            self.assertEqual(command[0], exe)
            self.assertEqual(len(command), 2)
            self.assertTrue(command[1].startswith('file:///C:/Users/Me/Docs/Annual%20report%20%232.pdf'), command[1])
            self.assertTrue(command[1].endswith('#page=7'))

    def test_non_ascii_path_is_escaped_in_the_uri(self):
        command = opener.command_for(r'C:\x\msedge.exe', r'C:\Users\Zoë\Dokumente\Übersicht 日本.pdf', {'page': 2})
        self.assertTrue(command[1].isascii())
        self.assertIn('%C3%9Cbersicht', command[1])
        self.assertTrue(command[1].endswith('.pdf#page=2'))

    def test_pdf_readers(self):
        self.assertEqual(opener.command_for(r'C:\a\SumatraPDF.exe', PDF, {'page': 3}), [r'C:\a\SumatraPDF.exe', '-page', '3', PDF])
        for exe in (r'C:\a\Acrobat.exe', r'C:\a\AcroRd32.exe'):
            self.assertEqual(opener.command_for(exe, PDF, {'page': 3}), [exe, '/A', 'page=3', PDF])

    def test_video_players(self):
        self.assertEqual(opener.command_for(r'C:\v\vlc.exe', VIDEO, {'seconds': 83.7}), [r'C:\v\vlc.exe', '--start-time=83', VIDEO])
        self.assertEqual(opener.command_for(r'C:\v\mpv.exe', VIDEO, {'seconds': 0}), [r'C:\v\mpv.exe', '--start=0', VIDEO])

    def test_no_command_when_the_app_or_place_does_not_fit(self):
        for exe in (r'C:\o\POWERPNT.EXE', r'C:\o\WINWORD.EXE', r'C:\x\notepad.exe', None, ''):
            self.assertIsNone(opener.command_for(exe, PDF, {'page': 3}))
        self.assertIsNone(opener.command_for(r'C:\o\POWERPNT.EXE', r'C:\d\deck.pptx', {'slide': 4}))
        self.assertIsNone(opener.command_for(r'C:\a\SumatraPDF.exe', PDF, {}))
        self.assertIsNone(opener.command_for(r'C:\a\SumatraPDF.exe', PDF, {'page': 0}))
        self.assertIsNone(opener.command_for(r'C:\a\SumatraPDF.exe', PDF, {'page': '3; calc'}))
        self.assertIsNone(opener.command_for(r'C:\a\SumatraPDF.exe', VIDEO, {'page': 3}))  # a page means nothing for a video
        self.assertIsNone(opener.command_for(r'C:\v\vlc.exe', PDF, {'seconds': 5}))
        self.assertIsNone(opener.command_for(r'C:\v\vlc.exe', VIDEO, {'page': 5}))
        self.assertIsNone(opener.command_for(r'C:\v\vlc.exe', '-relative.mp4', {'seconds': 5}))  # only absolute paths

    def test_open_at_launches_the_command_or_falls_back(self):
        plain = mock.Mock()
        with mock.patch.object(opener, 'default_handler', return_value=r'C:\a\SumatraPDF.exe'), \
                mock.patch.object(opener.subprocess, 'Popen') as popen:
            self.assertTrue(opener.open_at(PDF, {'page': 3}, plain=plain))
            self.assertEqual(popen.call_args.args[0], [r'C:\a\SumatraPDF.exe', '-page', '3', PDF])
            self.assertFalse(popen.call_args.kwargs.get('shell'))
            plain.assert_not_called()
        with mock.patch.object(opener, 'default_handler', return_value=r'C:\o\POWERPNT.EXE'), mock.patch.object(opener.subprocess, 'Popen') as popen:
            self.assertFalse(opener.open_at(PDF, {'page': 3}, plain=plain))
            popen.assert_not_called()
            plain.assert_called_once_with(PDF)
        plain.reset_mock()
        with mock.patch.object(opener, 'default_handler', return_value=r'C:\a\SumatraPDF.exe'), \
                mock.patch.object(opener.subprocess, 'Popen', side_effect=OSError('gone')):
            self.assertFalse(opener.open_at(PDF, {'page': 3}, plain=plain))
            plain.assert_called_once_with(PDF)

    def test_default_handler_is_read_only_and_safe(self):
        result = opener.default_handler(r'C:\nothing\file.pdf')  # whatever this machine has, or None
        self.assertTrue(result is None or isinstance(result, str))


if __name__ == '__main__':
    unittest.main()
