"""Model and engine downloads (alfred.models) and their Settings rows and first-run question (alfred.ui).

No internet: a small HTTP server on 127.0.0.1 serves fake files, with Range support. No test starts a llama-server
(runtime.program_dir is patched, PROGRAM_OVERRIDE is None), and every ALFRED_HOME is a temporary folder.
"""
import hashlib
import io
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import models, runtime  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.seen.append((self.path, self.headers.get('Range')))
        data = server.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        wanted = self.headers.get('Range')
        if wanted and server.ranges:
            start = int(wanted.split('=')[1].split('-')[0])
            chunk = data[start:]
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
        else:
            chunk = data
            self.send_response(200)
        self.send_header('Content-Length', str(len(chunk)))
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except OSError:
            pass  # the client stopped (a cancelled download)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.files, self.seen, self.ranges = {}, [], True

    def handle_error(self, request, client_address):
        pass

    def url(self, path):
        return f'http://127.0.0.1:{self.server_address[1]}{path}'


def fake_file(key, content, urls, sha256=None):
    return models.ModelFile(key, models.FILES[key].role, f'Fake {key}', 'fake-model', f'{key}.gguf', len(content),
                            sha256 or hashlib.sha256(content).hexdigest(), 'none', tuple(urls))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / 'home'
        self.home.mkdir()
        self.server = Server()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.stopped = []
        env = {'ALFRED_HOME': str(self.home), 'OLLAMA_MODELS': str(self.root / 'ollama'), 'USERPROFILE': str(self.root / 'user')}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(runtime, 'program_dir', lambda home=None: None),
                   mock.patch.object(runtime, 'PROGRAM_OVERRIDE', None), mock.patch.object(models, 'BLOCK', 1024),
                   mock.patch.object(runtime, 'stop', lambda role, settings=None: self.stopped.append(role))]
        os.environ.pop('ALFRED_MODELS_DIR', None)
        os.environ.pop('ALFRED_LLAMA_DIR', None)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.content = os.urandom(64 * 1024)
        self.server.files['/text.gguf'] = self.content

    def use(self, key='embed-text', content=None, urls=None, sha256=None):
        content = self.content if content is None else content
        file = fake_file(key, content, urls if urls is not None else [self.server.url('/text.gguf')], sha256)
        patch = mock.patch.dict(models.FILES, {key: file})
        patch.start()
        self.addCleanup(patch.stop)
        return file


class DownloadTests(Base):
    def test_full_download_is_checked_and_put_in_place(self):
        self.use()
        seen = []
        path = models.download('embed-text', self.home, lambda done, total: seen.append((done, total)))
        self.assertEqual(path, models.own_path('embed-text', self.home))
        self.assertEqual(path.read_bytes(), self.content)
        self.assertFalse(path.with_name(path.name + '.part').exists())
        self.assertEqual(seen[-1], (len(self.content), len(self.content)))
        self.assertEqual(models.find('embed-text', self.home), path)
        self.assertEqual(models.state('embed-text', self.home)['where'], 'alfred')

    def test_cancel_keeps_the_part_file_and_the_next_download_resumes(self):
        self.use()
        cancel = threading.Event()

        def progress(done, total):
            if done >= 8 * 1024:
                cancel.set()
        with self.assertRaises(models.DownloadCancelled):
            models.download('embed-text', self.home, progress, cancel)
        part = models._part(models.own_path('embed-text', self.home))
        kept = part.stat().st_size
        self.assertTrue(0 < kept < len(self.content))
        self.assertFalse(models.own_path('embed-text', self.home).exists())
        self.assertEqual(models.state('embed-text', self.home)['part'], kept)
        path = models.download('embed-text', self.home)
        self.assertEqual(self.server.seen[-1][1], f'bytes={kept}-')
        self.assertEqual(path.read_bytes(), self.content)

    def test_a_server_without_range_starts_again(self):
        self.use()
        part = models._part(models.own_path('embed-text', self.home))
        part.parent.mkdir(parents=True)
        part.write_bytes(b'junk' * 100)
        self.server.ranges = False
        self.assertEqual(models.download('embed-text', self.home).read_bytes(), self.content)

    def test_hash_mismatch_deletes_the_part_file(self):
        self.use(sha256='0' * 64)
        with self.assertRaisesRegex(models.DownloadError, 'checksum'):
            models.download('embed-text', self.home)
        own = models.own_path('embed-text', self.home)
        self.assertFalse(own.exists())
        self.assertFalse(models._part(own).exists())

    def test_the_next_address_is_tried(self):
        self.use(urls=['http://example.com/text.gguf', self.server.url('/missing.gguf'), self.server.url('/text.gguf')])
        self.assertEqual(models.download('embed-text', self.home).read_bytes(), self.content)
        self.assertEqual([path for path, _ in self.server.seen], ['/missing.gguf', '/text.gguf'])  # never the http address

    def test_every_address_failing_says_so(self):
        self.use(urls=[self.server.url('/missing.gguf')])
        with self.assertRaisesRegex(models.DownloadError, 'HTTP 404'):
            models.download('embed-text', self.home)

    def test_not_enough_disk_space_refuses(self):
        self.use()
        with mock.patch.object(models, '_free_bytes', lambda path: 1000):
            with self.assertRaisesRegex(models.DownloadError, 'Not enough free disk space'):
                models.download('embed-text', self.home)
        self.assertEqual(self.server.seen, [])

    def test_the_answer_model_has_no_address(self):
        state = models.state('answer', self.home)
        self.assertEqual((state['where'], state['downloadable']), ('missing', False))
        with self.assertRaisesRegex(models.DownloadError, 'no download address'):
            models.download('answer', self.home)


class LookupAndRemoveTests(Base):
    def test_models_dir_by_name_and_by_blob_after_own_copy(self):
        file = self.use()
        folder = self.root / 'dev-models'
        folder.mkdir()
        (folder / file.name).write_bytes(self.content)
        with mock.patch.dict(os.environ, {'ALFRED_MODELS_DIR': str(folder)}):
            found = models.find('embed-text', self.home)
            self.assertEqual(found, folder / file.name)
            self.assertEqual(models.state('embed-text', self.home)['where'], 'external')
            (folder / file.name).rename(folder / f'sha256-{file.sha256}')
            self.assertEqual(models.find('embed-text', self.home), folder / f'sha256-{file.sha256}')
            own = models.own_path('embed-text', self.home)
            own.parent.mkdir(parents=True)
            own.write_bytes(self.content)
            self.assertEqual(models.find('embed-text', self.home), own)

    def test_models_dir_comes_before_ollama(self):
        file = self.use()
        blobs = self.root / 'ollama' / 'blobs'
        blobs.mkdir(parents=True)
        (blobs / f'sha256-{file.sha256}').write_bytes(self.content)
        self.assertEqual(models.state('embed-text', self.home)['where'], 'ollama')
        folder = self.root / 'dev-models'
        folder.mkdir()
        (folder / file.name).write_bytes(self.content)
        with mock.patch.dict(os.environ, {'ALFRED_MODELS_DIR': str(folder)}):
            self.assertEqual(models.find('embed-text', self.home), folder / file.name)

    def test_remove_leaves_an_external_copy_alone(self):
        file = self.use()
        folder = self.root / 'dev-models'
        folder.mkdir()
        (folder / file.name).write_bytes(self.content)
        with mock.patch.dict(os.environ, {'ALFRED_MODELS_DIR': str(folder)}):
            self.assertFalse(models.remove('embed-text', self.home))
            self.assertTrue((folder / file.name).exists())
            models.download('embed-text', self.home)
            self.assertTrue(models.remove('embed-text', self.home))
            self.assertFalse(models.own_path('embed-text', self.home).exists())
            self.assertTrue((folder / file.name).exists())
            self.assertEqual(self.stopped, ['search', 'index'])
            self.assertEqual(models.find('embed-text', self.home), folder / file.name)


def engine_zip(extra=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as bundle:
        for name in ('llama-server.exe', 'ggml.dll', 'LICENSE-curl', 'llama-bench.exe', 'README.md') + tuple(extra):
            bundle.writestr(name, f'fake {name}')
    return buffer.getvalue()


class EngineTests(Base):
    def build(self, data, sha256=None):
        build = dict(models.BUILD, size=len(data), unpacked=1000, urls=(self.server.url('/engine.zip'),),
                     sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256)
        self.server.files['/engine.zip'] = data
        patch = mock.patch.object(models, 'BUILD', build)
        patch.start()
        self.addCleanup(patch.stop)

    def test_download_unpacks_only_the_server_libraries_and_licences(self):
        self.build(engine_zip())
        folder = models.download_engine(self.home)
        self.assertEqual(folder, self.home / 'runtime' / models.BUILD['name'])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['LICENSE-curl', 'ggml.dll', 'llama-server.exe'])
        self.assertFalse((self.home / 'runtime' / models.BUILD['zip']).exists())
        self.assertEqual([p.name for p in (self.home / 'runtime').iterdir()], [models.BUILD['name']])  # no temporary folder left
        with mock.patch.object(runtime, 'program_dir', lambda home=None: folder):
            self.assertEqual(models.state(models.ENGINE, self.home)['where'], 'alfred')
            self.assertTrue(models.remove_engine(self.home))
        self.assertFalse(folder.exists())

    def test_a_path_leaving_the_folder_refuses_the_zip(self):
        for bad in ('../evil.dll', 'sub/../../evil.dll', 'C:/evil.dll', '/evil.dll'):
            with self.subTest(bad):
                archive = self.root / 'bad.zip'
                archive.write_bytes(engine_zip([bad]))
                final = self.root / 'unpacked' / 'llama'
                with self.assertRaisesRegex(models.DownloadError, 'unsafe path'):
                    models.unpack_engine(archive, final)
                self.assertFalse(final.exists())
                self.assertFalse((self.root / 'unpacked' / 'evil.dll').exists())
                self.assertFalse((self.root / 'evil.dll').exists())
                self.assertEqual(list((self.root / 'unpacked').iterdir()), [])

    def test_no_pinned_hash_means_no_download(self):
        self.build(engine_zip(), sha256='')
        self.assertFalse(models.state(models.ENGINE, self.home)['downloadable'])
        with self.assertRaisesRegex(models.DownloadError, 'no pinned checksum'):
            models.download_engine(self.home)
        self.assertEqual(self.server.seen, [])

    def test_the_real_build_is_pinned(self):
        self.assertEqual(len(models.BUILD['sha256']), 64)
        self.assertTrue(all(url.startswith('https://') for url in models.BUILD['urls']))

    def test_first_run_needs(self):
        self.use()
        self.build(engine_zip())
        self.assertEqual(models.first_run_needs(self.home), [models.ENGINE, 'embed-text'])
        models.download('embed-text', self.home)
        self.assertEqual(models.first_run_needs(self.home), [models.ENGINE])
        with mock.patch.object(runtime, 'program_dir', lambda home=None: self.root):
            self.assertEqual(models.first_run_needs(self.home), [])


class FirstRunTests(unittest.TestCase):
    """The first-run question and the Settings rows, on a headless window."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(os.path.realpath(self.tmp.name))
        self.patches = [mock.patch.dict(os.environ, {'ALFRED_HOME': str(base / 'home'), 'OLLAMA_MODELS': str(base / 'ollama'),
                                                     'USERPROFILE': str(base / 'user')}),
                        mock.patch.object(runtime, 'program_dir', lambda home=None: None),
                        mock.patch.object(runtime, 'PROGRAM_OVERRIDE', None)]
        for patch in self.patches:
            patch.start()
        from alfred import ai, ui
        self.ui = ui
        self.patches.append(mock.patch.object(ai.Embedder, 'from_settings', staticmethod(lambda settings: None)))
        self.patches[-1].start()
        self.win = ui.AlfredWindow()
        self.started = []
        self.patches.append(mock.patch.object(ui.ModelDownloads, 'start', lambda downloads, key: self.started.append(key)))
        self.patches[-1].start()

    def tearDown(self):
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(10000)
        self.app.processEvents()
        self.win.index.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.win.deleteLater()
        self.tmp.cleanup()

    def settle(self):
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(10000)
        for _ in range(5):
            self.app.processEvents()

    def offer(self, needs, answer):
        from PySide6.QtWidgets import QMessageBox
        asked = []
        reply = QMessageBox.Yes if answer else QMessageBox.No
        with mock.patch.object(models, 'first_run_needs', lambda home=None: list(needs)), \
                mock.patch.object(self.ui, 'ask_box', lambda *args: (asked.append(args[2]), reply)[1]):
            self.win.offer_model_download()
            self.settle()
        return asked

    def test_asked_once_when_something_is_missing(self):
        asked = self.offer([models.ENGINE, 'embed-text'], answer=False)
        self.assertEqual(len(asked), 1)
        total = models.BUILD['size'] + models.FILES['embed-text'].size
        self.assertIn(f'Download the search model and engine ({total / 1e6:,.0f} MB)?', asked[0])
        self.assertEqual(self.started, [])
        self.assertTrue(self.win.settings['model_download_asked'])
        self.assertEqual(self.offer([models.ENGINE, 'embed-text'], answer=True), [])  # No is remembered
        self.assertEqual(self.started, [])

    def test_yes_starts_both_downloads(self):
        self.offer([models.ENGINE, 'embed-text'], answer=True)
        self.assertEqual(self.started, [models.ENGINE, 'embed-text'])

    def test_not_asked_when_both_are_here(self):
        self.assertEqual(self.offer([], answer=True), [])
        self.assertFalse(self.win.settings['model_download_asked'])

    def test_no_download_offered_for_the_answer_model(self):
        view = self.win.settings_view
        view.load_files()
        self.settle()
        self.assertEqual(view.file_states['answer']['downloadable'], False)
        self.assertTrue(view.file_button['answer'].isHidden())
        self.assertIn('no download offered', view.file_status['answer'].text())
        self.assertFalse(view.file_button['embed-text'].isHidden())  # the search model can be downloaded
        self.assertIn('Not downloaded', view.file_status['embed-text'].text())

    def test_status_line_text_is_short(self):
        downloads = self.ui.ModelDownloads(self.win)
        downloads.progress = {models.ENGINE: (10_000_000, models.BUILD['size']), 'embed-text': (0, models.FILES['embed-text'].size)}
        self.assertLessEqual(len(downloads.status_text()), 60)
        downloads.progress = {'embed-pictures': (500_000_000, models.FILES['embed-pictures'].size)}
        self.assertLessEqual(len(downloads.status_text()), 60)


if __name__ == '__main__':
    unittest.main()
