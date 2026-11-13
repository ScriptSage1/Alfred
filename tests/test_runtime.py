"""The llama.cpp runtime (alfred.runtime, alfred.models) and the Embedder on top of it.

Every test but the last runs against tests/fake_llama.py, a small stand-in for llama-server.exe: no model, no GPU.
The last test is opt-in: set ALFRED_LLAMA_DIR to a llama.cpp build to run the real server with the real text model
(Ollama's copy of it) on the CPU. Every ALFRED_HOME is a temporary folder.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alfred import ai, config, models, runtime  # noqa: E402
from alfred.index import PICTURE_VERSION, Index  # noqa: E402

FAKE = [sys.executable, str(HERE / 'fake_llama.py')]
GPU = 'Vulkan0: Fake GPU (8192 MiB, 8000 MiB free)'


def unit_length(vector):
    return math.sqrt(sum(x * x for x in vector))


def reset_runtime():
    runtime.stop_all()
    runtime._clients.clear()
    runtime._failures.clear()
    runtime._forced_cpu.clear()
    runtime._build_cache.clear()
    runtime._starters.clear()


class FakeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.home = self.dir / 'home'
        self.home.mkdir()
        patches = [mock.patch.dict(os.environ, {'ALFRED_HOME': str(self.home)}), mock.patch.object(runtime, 'PROGRAM_OVERRIDE', FAKE),
                   mock.patch.object(models, 'find', lambda key, home=None: self.home / f'{key}.gguf')]
        os.environ.pop('FAKE_LLAMA_DEVICES', None)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_runtime)
        self.settings = config.load(self.home)
        self.settings.update(ocr_enabled=False)
        self.embedder = ai.Embedder(self.settings)

    def starts(self):
        try:
            lines = (self.home / 'fake_starts.jsonl').read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        return [json.loads(line)['args'] for line in lines]

    def devices(self, text):
        patch = mock.patch.dict(os.environ, {'FAKE_LLAMA_DEVICES': text})
        patch.start()
        self.addCleanup(patch.stop)


class TextTests(FakeBase):
    def test_query_vector_has_768_numbers_of_unit_length_and_the_key_stays_off_the_command_line(self):
        vector = self.embedder.embed_query('tax return 2024')
        self.assertEqual(len(vector), 768)
        self.assertAlmostEqual(unit_length(vector), 1.0, places=5)
        record = runtime.read_record(self.home, 'search')
        [args] = self.starts()
        self.assertNotIn(record['key'], ' '.join(args))
        self.assertEqual(args[args.index('--device') + 1], 'none')  # search runs on the CPU by default
        self.assertEqual(args[args.index('-t') + 1], '4')
        self.assertEqual(args[args.index('-c') + 1], '2048')
        self.assertIn('--sleep-idle-seconds', args)
        with self.assertRaises(runtime.HttpError) as caught:  # without the key the server refuses
            runtime._request(record['port'], 'POST', '/v1/embeddings', {'input': ['x']})
        self.assertEqual(caught.exception.status, 401)
        self.assertTrue((self.home / 'logs' / 'llama-search.log').exists())

    def test_signature_and_picture_version(self):
        self.assertEqual(self.embedder.signature(), 'ollama:embeddinggemma-2:prompt-v1')
        self.assertEqual(ai.Embedder({'embed_model': 'nomic-embed-text'}).signature(), 'ollama:embeddinggemma-2:prompt-v1')
        self.assertEqual(PICTURE_VERSION, 'picture-v2:512')

    def test_too_long_input_is_sent_alone_and_cut_until_accepted(self):
        chinese = '中文文本很长' * 1000  # 6,000 characters: far more than a 2,048-token slot
        vectors = self.embedder.embed_documents([chinese, 'harbour pilot schedule'], ['long', 'short'])
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            self.assertEqual(len(vector), 768)
            self.assertAlmostEqual(unit_length(vector), 1.0, places=5)
        self.assertEqual(vectors[1], self.embedder.embed_documents(['harbour pilot schedule'], ['short'])[0])
        self.assertEqual(len(self.embedder.embed_query('中文' * 3000)), 768)

    def test_search_only_never_starts_a_server_with_the_picture_projector_or_the_answer_model(self):
        from PIL import Image
        folder = self.dir / 'Tracked'
        folder.mkdir()
        (folder / 'boat.txt').write_text('Kayak hull repair: sand, patch and seal the leaking seam.', encoding='utf-8')
        (folder / 'garden.txt').write_text('Garden plan: repair the fence before spring planting.', encoding='utf-8')
        Image.new('RGB', (64, 64), 'red').save(folder / 'red.png')
        self.settings.update(tracked_folders=[str(folder)], picture_search=False, provider='local', model='')  # answers off
        index = Index(self.settings)
        self.addCleanup(index.close)
        summary = index.update(embedder=self.embedder)
        self.assertGreater(summary['embedded'], 0)
        self.assertEqual(index.search('kayak hull repair', embedder=self.embedder, mode='hybrid', include_weak=True)[0]['filename'], 'boat.txt')
        starts = self.starts()
        self.assertTrue(starts)
        self.assertFalse(any('--mmproj' in args for args in starts), starts)
        self.assertTrue(all('--no-mmproj' in args for args in starts))
        self.assertEqual(len(starts), 1)  # a small job goes through the search server: no index server
        self.assertFalse(any(str(self.home / 'answer.gguf') in args for args in starts))
        ai.warm_up(self.settings)  # opening the panel wakes search only
        self.assertEqual(len(self.starts()), 1)
        self.assertIsNone(runtime.read_record(self.home, 'answer'))

    def test_a_larger_job_uses_the_index_server_which_exits_after_its_linger(self):
        self.assertEqual(self.embedder.plan(4), 4)
        self.assertEqual(self.embedder.role, 'search')
        self.assertEqual(self.embedder.plan(40), 16)
        self.assertEqual(self.embedder.role, 'index')
        self.embedder.embed_documents([f'passage {n}' for n in range(16)])
        server = runtime._clients[(str(self.home), 'index')].server
        self.assertTrue(server.alive())
        args = self.starts()[-1]
        self.assertEqual(args[args.index('--device') + 1], 'none')  # no graphics card here
        self.assertEqual(runtime.role_for_job(2, settings=self.settings), 'index')  # the running index server keeps the work
        runtime.release('index', self.settings, after=0.2)
        deadline = time.monotonic() + 10
        while server.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(server.alive())
        self.assertIsNone(runtime.read_record(self.home, 'index'))

    def test_a_new_run_cancels_the_linger(self):
        self.embedder.plan(40)
        self.embedder.embed_documents(['one', 'two'])
        server = runtime._clients[(str(self.home), 'index')].server
        runtime.release('index', self.settings, after=0.3)
        runtime.client('index', self.settings)  # a new run asks for it
        time.sleep(0.6)
        self.assertTrue(server.alive())


class PictureTests(FakeBase):
    def pictures(self):
        from PIL import Image
        return [Image.new('RGB', (96, 96), colour) for colour in ('red', 'blue')]

    def test_pictures_use_the_index_server_and_the_marker_is_read_again_on_400(self):
        first = self.embedder.embed_pictures(self.pictures())
        self.assertEqual([len(v) for v in first], [768, 768])
        self.assertAlmostEqual(unit_length(first[0]), 1.0, places=5)
        self.assertNotEqual(first[0], first[1])
        current = runtime._clients[(str(self.home), 'index')]
        old = current.marker
        runtime._request(current.port, 'POST', '/_test/marker', {})  # the server's marker changes (a restart would do this)
        again = self.embedder.embed_pictures(self.pictures())
        self.assertEqual(again, first)
        self.assertNotEqual(current.marker, old)
        [args] = self.starts()
        self.assertIn('--mmproj', args)
        self.assertIn('--no-mmproj-offload', args)  # on the CPU

    def test_a_query_never_loads_the_projector_even_with_picture_search_on(self):
        self.embedder.embed_query('sunset over the harbour')
        self.assertTrue(self.embedder.supports_pictures())
        self.assertFalse(any('--mmproj' in args for args in self.starts()))

    def test_a_text_only_index_server_is_restarted_with_the_projector_when_pictures_come(self):
        self.embedder.plan(40)
        self.embedder.embed_documents(['one', 'two'])
        self.embedder.embed_pictures(self.pictures()[:1])
        starts = self.starts()
        self.assertEqual(['--mmproj' in args for args in starts], [False, True])


class LifeTests(FakeBase):
    def test_restart_after_the_server_is_killed(self):
        before = self.embedder.embed_query('harbour')
        current = runtime._clients[(str(self.home), 'search')]
        pid = current.pid
        current.server.process.kill()
        current.server.process.wait()
        after = self.embedder.embed_query('harbour')
        self.assertEqual(before, after)
        self.assertNotEqual(runtime._clients[(str(self.home), 'search')].pid, pid)
        self.assertEqual(len(self.starts()), 2)

    def test_threads_starting_at_once_share_one_server(self):
        found = []
        threads = [threading.Thread(target=lambda: found.append(runtime.client('search', self.settings).pid)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(set(found)), 1)
        self.assertEqual(len(self.starts()), 1)

    def child(self, seconds):
        env = dict(os.environ, ALFRED_HOME=str(self.home), FAKE_LLAMA_LOAD_SECONDS='1')
        return subprocess.Popen([sys.executable, str(HERE / 'runtime_child.py'), str(seconds)], stdout=subprocess.PIPE, text=True, env=env,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_two_processes_starting_at_once_share_one_server(self):
        children = [self.child(4), self.child(4)]
        pids = [int(child.stdout.readline()) for child in children]
        for child in children:
            child.wait(30)
            child.stdout.close()
        self.assertEqual(pids[0], pids[1])
        self.assertEqual(len(self.starts()), 1)

    def test_a_server_dies_with_its_owner(self):
        owner = self.child(60)
        pid = int(owner.stdout.readline())
        self.assertIsNotNone(runtime.process_created(pid))
        owner.kill()  # a crash: no clean-up code runs in the owner
        owner.wait(10)
        owner.stdout.close()
        deadline = time.monotonic() + 10
        while runtime.process_created(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(runtime.process_created(pid))
        self.assertIsNone(runtime._live_record(self.home, 'search'))  # the stale record is not trusted
        self.embedder.embed_query('still works')  # this process starts its own


class GpuTests(FakeBase):
    def test_failed_check_moves_indexing_to_the_cpu_and_is_remembered(self):
        self.devices(GPU)
        with mock.patch.object(runtime, 'reference', lambda: {'model_sha256': models.FILES['embed-text'].sha256,
                                                                'prompts': ['a', 'b'], 'vectors': [[1.0] + [0.0] * 767] * 2}):
            self.embedder.plan(40)
            self.embedder.embed_documents(['one', 'two'])
        starts = self.starts()
        self.assertEqual([args[args.index('--device') + 1] for args in starts], ['Vulkan0', 'none'])
        checks = json.loads((self.home / 'run' / 'gpu_check.json').read_text(encoding='utf-8'))
        [verdict] = checks.values()
        self.assertFalse(verdict['passed'])
        runtime.stop('index', self.settings)
        self.embedder.plan(40)
        self.embedder.embed_documents(['three'])  # the verdict is stored: no second check
        self.assertEqual([args[args.index('--device') + 1] for args in self.starts()], ['Vulkan0', 'none', 'none'])

    def test_passed_check_keeps_the_server_on_the_gpu_with_four_slots(self):
        self.devices(GPU)
        prompts = ['title: none | text: a', 'task: search result | query: b']
        result = runtime.call('search', '/v1/embeddings', {'input': prompts}, self.settings)
        vectors = [d['embedding'] for d in sorted(result['data'], key=lambda d: d['index'])]
        runtime.stop('search', self.settings)
        with mock.patch.object(runtime, 'reference', lambda: {'model_sha256': models.FILES['embed-text'].sha256,
                                                                'prompts': prompts, 'vectors': vectors}):
            self.embedder.plan(40)
            self.embedder.embed_documents(['one', 'two'])
        args = self.starts()[-1]
        self.assertEqual(args[args.index('--device') + 1], 'Vulkan0')
        self.assertEqual((args[args.index('-c') + 1], args[args.index('-np') + 1]), ('8192', '4'))
        self.assertEqual(len(self.starts()), 2)  # the checked server is kept and used
        self.assertEqual(runtime._clients[(str(self.home), 'index')].device, 'Vulkan0')

    def test_a_server_that_cannot_start_on_the_card_stores_no_verdict(self):
        self.devices(GPU)
        statuses = []
        with mock.patch.dict(os.environ, {'FAKE_LLAMA_FAIL_DEVICE': 'Vulkan0'}), mock.patch.object(runtime, 'status_hook', statuses.append):
            self.embedder.plan(40)
            self.embedder.embed_documents(['one'])
        self.assertEqual([args[args.index('--device') + 1] for args in self.starts()], ['none'])  # the failed start is not logged
        self.assertFalse((self.home / 'run' / 'gpu_check.json').exists())
        self.assertEqual(statuses, ['Preparing the graphics card (once)…', None])
        self.assertIn('not checked yet', runtime.gpu_summary(self.settings))

    def test_too_little_free_graphics_memory_runs_on_the_cpu_without_a_check(self):
        self.devices('Vulkan0: Fake GPU (8192 MiB, 700 MiB free)')
        self.embedder.plan(40)
        self.embedder.embed_documents(['one'])
        args = self.starts()[-1]
        self.assertEqual(args[args.index('--device') + 1], 'none')
        self.assertFalse((self.home / 'run' / 'gpu_check.json').exists())

    def test_search_stays_on_the_cpu_unless_asked(self):
        self.devices(GPU)
        self.embedder.embed_query('x')
        self.assertEqual(self.starts()[-1][self.starts()[-1].index('--device') + 1], 'none')

    def test_two_gpu_failures_in_ten_minutes_move_the_role_to_the_cpu(self):
        self.devices(GPU)
        key = (str(self.home), 'index')
        with mock.patch.object(runtime, 'gpu_verdict', lambda home, device: {'passed': True}):
            self.embedder.plan(40)
            self.embedder.embed_documents(['one'])
            for _ in range(2):
                runtime._clients[key].server.process.kill()
                runtime._clients[key].server.process.wait()
                self.embedder.embed_documents(['again'])
        self.assertEqual([args[args.index('--device') + 1] for args in self.starts()], ['Vulkan0', 'Vulkan0', 'none'])
        self.assertIn(key, runtime._forced_cpu)


class AnswerTests(FakeBase):
    def setUp(self):
        super().setUp()
        from alfred import providers
        self.settings.update(provider='local', model='gemma3:4b')
        self.local = providers.for_settings(self.settings)

    def test_answers_stream_through_the_local_provider(self):
        self.assertEqual((self.local.kind, self.local.label, self.local.cloud), ('local', 'On this PC', False))
        self.assertEqual(self.local.status()['state'], 'ready')
        self.assertEqual(self.local.models(), [('gemma3:4b', 'Gemma 3 4B')])
        pieces = []
        text = self.local.stream([{'role': 'user', 'content': 'hi'}], on_token=pieces.append)
        self.assertEqual((text, pieces), ('A fake answer.', ['A ', 'fake ', 'answer.']))
        [args] = self.starts()
        self.assertIn(str(self.home / 'answer.gguf'), args)
        self.assertIn('--no-mmproj', args)
        self.assertNotIn('--embedding', args)
        self.assertEqual((args[args.index('-c') + 1], args[args.index('-np') + 1], args[args.index('--device') + 1]), ('10240', '1', 'none'))
        record = runtime.read_record(self.home, 'answer')
        self.assertNotIn(record['key'], ' '.join(args))

    def test_the_answer_server_stops_after_its_keep_alive(self):
        with mock.patch.object(runtime, 'keep_alive_seconds', lambda settings: 0.3):
            self.local.stream([{'role': 'user', 'content': 'hi'}])
            server = runtime._clients[(str(self.home), 'answer')].server
            self.assertTrue(server.alive())
            deadline = time.monotonic() + 10
            while server.alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(server.alive())
        self.assertIsNone(runtime.read_record(self.home, 'answer'))

    def test_a_killed_answer_server_is_started_again(self):
        self.local.stream([{'role': 'user', 'content': 'hi'}])
        runtime._clients[(str(self.home), 'answer')].server.process.kill()
        self.assertEqual(self.local.stream([{'role': 'user', 'content': 'again'}]), 'A fake answer.')
        self.assertEqual(len(self.starts()), 2)

    def test_passage_contexts_and_query_rewrites_use_the_answer_server(self):
        from alfred import rag
        self.settings['contextual_chunks'] = True
        writer = ai.Contextualizer.from_settings(self.settings)
        self.assertEqual(writer.situate('report.txt', 'A long document.', 'A passage.'), 'A fake answer.')
        complete = rag.completer(self.settings, self.local)
        self.assertEqual(complete([{'role': 'user', 'content': 'rewrite'}]), 'A fake answer.')
        self.assertEqual(len(self.starts()), 1)

    def test_answer_context_and_device(self):
        self.settings.update(answer_context=4096)
        self.devices(GPU)
        self.local.stream([{'role': 'user', 'content': 'hi'}])
        args = self.starts()[-1]
        self.assertEqual((args[args.index('-c') + 1], args[args.index('--device') + 1], args[args.index('-ngl') + 1]), ('4096', 'Vulkan0', 'all'))

    def test_the_answer_model_stays_off_a_card_that_failed_or_is_full(self):
        self.devices(GPU)
        with mock.patch.object(runtime, 'gpu_verdict', lambda home, device: {'passed': False, 'reason': 'vectors differ'}):
            self.local.stream([{'role': 'user', 'content': 'hi'}])
        runtime.stop('answer', self.settings)
        self.devices('Vulkan0: Fake GPU (8192 MiB, 3000 MiB free)')  # less than 3,900 MiB + the margin
        self.local.stream([{'role': 'user', 'content': 'hi'}])
        runtime.stop('answer', self.settings)
        self.settings['device'] = 'cpu'
        self.devices(GPU)
        self.local.stream([{'role': 'user', 'content': 'hi'}])
        self.assertEqual([args[args.index('--device') + 1] for args in self.starts()], ['none', 'none', 'none'])

    def test_a_missing_answer_model_says_so(self):
        with mock.patch.object(models, 'find', lambda key, home=None: None if key == 'answer' else self.home / f'{key}.gguf'):
            self.assertEqual(self.local.status()['state'], 'missing')
            self.assertIn('gemma3:4b', runtime.missing(self.settings, answer=True))
            self.assertIsNone(runtime.missing(self.settings))
            self.assertEqual(self.local.models(), [])


class DeviceTests(unittest.TestCase):
    def test_list_devices_is_parsed(self):
        text = 'Available devices:\n  Vulkan0: NVIDIA GeForce RTX 5070 Ti Laptop GPU (11944 MiB, 11176 MiB free)\n'
        with mock.patch.object(runtime, '_run', lambda home, *args, timeout=20: text):
            self.assertEqual(runtime.devices(), [{'id': 'Vulkan0', 'name': 'NVIDIA GeForce RTX 5070 Ti Laptop GPU',
                                                  'total_mib': 11944, 'free_mib': 11176}])
        with mock.patch.object(runtime, '_run', lambda home, *args, timeout=20: 'Available devices:\n'):
            self.assertEqual(runtime.devices(), [])


class SettingsTests(unittest.TestCase):
    def test_an_old_settings_file_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'settings.json').write_text(json.dumps({
                'start_with_windows_configured': True, 'embed_provider': 'ollama', 'embed_base_url': 'http://127.0.0.1:11434',
                'embed_model': 'nomic-embed-text', 'embed_device': 'cpu', 'embed_enabled': True}), encoding='utf-8')
            settings = config.load(home)
            self.assertEqual((settings['embed_device'], settings['search_on_gpu']), ('cpu', False))
            for gone in ('embed_provider', 'embed_base_url', 'embed_model'):
                self.assertNotIn(gone, settings)
            self.assertIsNotNone(ai.Embedder.from_settings(settings))
            settings.save()
            self.assertNotIn('embed_model', json.loads((home / 'settings.json').read_text(encoding='utf-8')))


class MigrationTests(unittest.TestCase):
    def load(self, stored):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        if stored is not None:
            (home / 'settings.json').write_text(json.dumps(dict(stored, start_with_windows_configured=True)), encoding='utf-8')
        return config.load(home)

    def test_a_new_install_defaults_to_the_model_on_this_pc_with_nothing_chosen(self):
        settings = self.load(None)
        self.assertEqual((settings['provider'], settings['model'], settings['answer_context']), ('local', '', 10240))
        for gone in ('ollama_autostart', 'ollama_context', 'base_url', 'context_model'):
            self.assertNotIn(gone, settings)

    def test_an_ollama_user_gets_no_model_chosen(self):
        settings = self.load({'provider': 'ollama', 'model': 'qwen3:8b', 'ollama_context': 16384, 'ollama_autostart': False,
                              'base_url': 'http://127.0.0.1:11434', 'context_model': 'gemma3:4b', 'keep_alive_minutes': 30})
        self.assertEqual((settings['provider'], settings['model'], settings['answer_context']), ('local', '', 16384))
        self.assertFalse(__import__('alfred.providers', fromlist=['x']).for_settings(settings).configured)
        self.assertEqual(settings['keep_alive_minutes'], 30)
        settings.save()
        stored = json.loads((settings.home / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['answer_context'], 16384)
        for gone in ('ollama_autostart', 'ollama_context', 'base_url', 'context_model'):
            self.assertNotIn(gone, stored)
        self.assertEqual(config.load(settings.home)['answer_context'], 16384)  # read once; the new key wins from now on

    def test_other_providers_and_a_new_answer_context_are_kept(self):
        settings = self.load({'provider': 'claude_code', 'model': 'sonnet', 'ollama_context': 4096, 'answer_context': 8192})
        self.assertEqual((settings['provider'], settings['model'], settings['answer_context']), ('claude_code', 'sonnet', 8192))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        content = b'not really a model'
        import hashlib
        self.file = models.ModelFile('embed-text', 'embed', 'test', 'test-model', 'test.gguf', len(content), hashlib.sha256(content).hexdigest(), 'none')
        self.blobs = self.root / 'ollama' / 'blobs'
        self.blobs.mkdir(parents=True)
        (self.blobs / f'sha256-{self.file.sha256}').write_bytes(content)
        for patch in (mock.patch.dict(models.FILES, {'embed-text': self.file}), mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(self.root / 'ollama')})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_ollama_copy_is_used_in_place_and_hashed_once(self):
        home = self.root / 'home'
        found = models.find('embed-text', home)
        self.assertEqual(found, self.blobs / f'sha256-{self.file.sha256}')
        self.assertEqual(models.source(found, home), 'ollama')
        with mock.patch.object(models, '_hash_file', side_effect=AssertionError('hashed twice')):
            self.assertEqual(models.find('embed-text', home), found)

    def test_wrong_content_or_size_is_not_used_and_own_copy_comes_first(self):
        home = self.root / 'home'
        (self.blobs / f'sha256-{self.file.sha256}').write_bytes(b'x' * self.file.size)  # same size, other content
        self.assertIsNone(models.find('embed-text', home))
        own = models.own_path('embed-text', home)
        own.parent.mkdir(parents=True)
        own.write_bytes(b'y' * self.file.size)
        self.assertEqual(models.find('embed-text', home), own)

    def test_the_listed_hashes_are_full_length(self):
        for file in models.FILES.values():
            self.assertEqual(len(file.sha256), 64)


@unittest.skipUnless(os.environ.get('ALFRED_LLAMA_DIR'), 'set ALFRED_LLAMA_DIR to a llama.cpp build to run the real server')
class RealServerTests(unittest.TestCase):
    """Opt-in: the real llama-server with the real text model (Ollama's copy), on the CPU, for a few seconds."""

    def test_real_text_embedding(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with mock.patch.dict(os.environ, {'ALFRED_HOME': folder}):
                settings = config.load(folder)
                embedder = ai.Embedder(settings)
                if not embedder.available():
                    self.skipTest(runtime.missing(settings))
                try:
                    vector = embedder.embed_query('tax return 2024')
                    self.assertEqual(len(vector), 768)
                    self.assertAlmostEqual(unit_length(vector), 1.0, places=5)
                    self.assertEqual(runtime._clients[(str(Path(folder)), 'search')].device, 'cpu')
                finally:
                    reset_runtime()


@unittest.skipUnless(os.environ.get('ALFRED_LLAMA_DIR') and os.environ.get('ALFRED_REAL_ANSWER'),
                     'set ALFRED_LLAMA_DIR and ALFRED_REAL_ANSWER=1 to start the real answer model (2.5 GB, on the GPU)')
class RealAnswerTests(unittest.TestCase):
    """Opt-in: the real answer server with gemma3:4b (Ollama's copy) answers one short question, then stops."""

    def test_real_answer(self):
        from alfred import providers
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with mock.patch.dict(os.environ, {'ALFRED_HOME': folder}):
                settings = config.load(folder)
                settings.update(provider='local', model='gemma3:4b')
                local = providers.for_settings(settings)
                if local.status()['state'] != 'ready':
                    self.skipTest(local.status()['detail'])
                try:
                    text = local.stream([{'role': 'user', 'content': 'Reply with one word: the colour of a clear daytime sky.'}])
                    self.assertTrue(text.strip())
                finally:
                    reset_runtime()


if __name__ == '__main__':
    unittest.main()
