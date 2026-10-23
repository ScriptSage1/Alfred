"""Chat provider tests with fake API servers and fake CLIs (no accounts or network needed).

Run:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import ai, keys, providers  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class FakeAPI(BaseHTTPRequestHandler):
    """OpenAI-style /models and /chat/completions, and Anthropic-style /v1/models and /v1/messages."""
    seen = []

    def log_message(self, *args):
        pass

    def _send(self, code, body, kind='application/json'):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeAPI.seen.append(('GET', self.path, dict(self.headers)))
        if self.headers.get('Authorization') == 'Bearer bad':
            return self._send(401, '{"error": {"message": "invalid key"}}')
        if self.path.endswith('/models') or self.path.startswith('/v1/models'):
            return self._send(200, json.dumps({'data': [{'id': 'gpt-test'}, {'id': 'text-embedding-3'}, {'id': 'claude-test', 'display_name': 'Claude Test'}]}))
        self._send(404, '{}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeAPI.seen.append(('POST', self.path, body))
        if self.path.endswith('/chat/completions'):
            chunks = [{'choices': [{'delta': {'content': piece}}]} for piece in ('Hello', ' from', ' OpenAI')]
            stream = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return self._send(200, stream, 'text/event-stream')
        if self.path == '/v1/messages':
            events = [{'type': 'message_start'}, {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'Hello'}},
                      {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': ' from Claude'}}, {'type': 'message_stop'}]
            stream = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events)
            return self._send(200, stream, 'text/event-stream')
        self._send(404, '{}')


def fake_cli(folder, name, script):
    """A .cmd that runs a Python script, standing in for claude.exe / codex.exe."""
    source = Path(folder) / f'{name}.py'
    source.write_text(textwrap.dedent(script), encoding='utf-8')
    command = Path(folder) / f'{name}.cmd'
    command.write_text(f'@"{sys.executable}" "{source}" %*\n', encoding='utf-8')
    return str(command)


CLAUDE_SCRIPT = '''
    import json, sys
    args = sys.argv[1:]
    if args[:2] == ['auth', 'status']:
        print(json.dumps({'loggedIn': MODE != 'signed_out', 'subscriptionType': 'pro', 'email': 'me@example.com'}))
        sys.exit(0)
    prompt = sys.stdin.read()
    open(LOG, 'w', encoding='utf-8').write(json.dumps({'args': args, 'prompt': prompt}))
    if MODE == 'limit':
        print(json.dumps({'type': 'result', 'is_error': True, 'result': 'Claude AI usage limit reached'}))
        sys.exit(1)
    for piece in ('Blue', ' sky'):
        print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': piece}}}), flush=True)
    print(json.dumps({'type': 'result', 'is_error': False, 'result': 'Blue sky'}))
'''

CODEX_SCRIPT = '''
    import json, sys
    args = sys.argv[1:]
    if args[:2] == ['login', 'status']:
        print('Logged in using ChatGPT' if MODE != 'signed_out' else 'Not logged in')
        sys.exit(0)
    prompt = sys.stdin.read()
    open(LOG, 'w', encoding='utf-8').write(json.dumps({'args': args, 'prompt': prompt}))
    print(json.dumps({'type': 'thread.started'}))
    print(json.dumps({'type': 'item.updated', 'item': {'type': 'agent_message', 'text': 'Hello'}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Hello from Codex'}}))
    print(json.dumps({'type': 'turn.completed'}))
'''


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = keyring.get_keyring()
        keyring.set_keyring(MemoryKeyring())
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeAPI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        keyring.set_keyring(cls.previous)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = str(Path(self.tmp.name) / 'cli-log.json')
        providers.forget_status()
        FakeAPI.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, kind, script, mode='ok'):
        exe = fake_cli(self.tmp.name, kind, f'MODE = {mode!r}\nLOG = {self.log!r}\n' + textwrap.dedent(script))
        provider = providers.for_settings({'provider': kind, 'model': 'sonnet' if kind == 'claude_code' else ''})
        provider.exe = lambda: exe
        provider.workdir = lambda: Path(self.tmp.name)
        return provider

    def test_keys_live_in_the_credential_store(self):
        keys.put('openai', '  sk-test  ')
        self.assertEqual(keys.get('openai'), 'sk-test')
        keys.put('openai', '')
        self.assertFalse(keys.has('openai'))

    def test_openai_compatible_lists_models_and_streams(self):
        settings = {'provider': 'compatible', 'model': 'gpt-test', 'compatible_base_url': self.url + '/v1'}
        provider = providers.for_settings(settings)
        self.assertFalse(provider.cloud)  # localhost
        self.assertIn(('gpt-test', 'gpt-test'), provider.models())
        tokens = []
        text = provider.stream([{'role': 'user', 'content': 'hi'}], on_token=tokens.append)
        self.assertEqual((text, tokens), ('Hello from OpenAI', ['Hello', ' from', ' OpenAI']))
        self.assertEqual(FakeAPI.seen[-1][2]['messages'], [{'role': 'user', 'content': 'hi'}])

    def test_openai_needs_a_key_and_reports_bad_ones(self):
        provider = providers.for_settings({'provider': 'openai', 'model': 'gpt-test'})
        self.assertEqual(provider.status()['state'], 'no_key')
        with self.assertRaises(ai.NotConfigured):
            provider.stream([{'role': 'user', 'content': 'hi'}])
        compatible = providers.for_settings({'provider': 'compatible', 'model': 'x', 'compatible_base_url': self.url + '/v1'})
        keys.put('compatible', 'bad')
        try:
            with self.assertRaisesRegex(ai.ProviderError, 'rejected'):
                providers._get_json(compatible.base + '/models', compatible.headers())
        finally:
            keys.delete('compatible')

    def test_anthropic_streams_with_system_prompt_apart(self):
        keys.put('anthropic', 'sk-ant-test')
        old = providers.ANTHROPIC_URL
        providers.ANTHROPIC_URL = self.url
        try:
            provider = providers.for_settings({'provider': 'anthropic', 'model': 'claude-test'})
            self.assertEqual(provider.status()['state'], 'ready')
            self.assertIn(('claude-test', 'Claude Test'), provider.models())
            text = provider.stream([{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'hi'}])
            self.assertEqual(text, 'Hello from Claude')
            body = FakeAPI.seen[-1][2]
            self.assertEqual((body['system'], body['messages']), ('Be brief.', [{'role': 'user', 'content': 'hi'}]))
        finally:
            providers.ANTHROPIC_URL = old
            keys.delete('anthropic')

    def test_claude_code_runs_locked_down_and_streams(self):
        provider = self.cli('claude_code', CLAUDE_SCRIPT)
        self.assertEqual(provider.status()['state'], 'ready')
        self.assertIn('Pro', provider.status()['detail'])
        tokens = []
        text = provider.stream([{'role': 'system', 'content': 'Answer briefly.'}, {'role': 'user', 'content': 'First?'},
                                {'role': 'assistant', 'content': 'One.'}, {'role': 'user', 'content': 'Sky colour?'}], on_token=tokens.append)
        self.assertEqual((text, tokens), ('Blue sky', ['Blue', ' sky']))
        call = json.loads(Path(self.log).read_text(encoding='utf-8'))
        args = call['args']
        self.assertEqual(args[args.index('--tools') + 1], '')  # every tool off
        for flag in ('-p', '--strict-mcp-config', '--no-session-persistence', '--include-partial-messages'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--system-prompt') + 1], 'Answer briefly.')
        self.assertIn('User: First?', call['prompt'])
        self.assertTrue(call['prompt'].endswith('User: Sky colour?'))

    def test_claude_code_signed_out_and_usage_limit(self):
        self.assertEqual(self.cli('claude_code', CLAUDE_SCRIPT, 'signed_out').status()['state'], 'signed_out')
        providers.forget_status()
        with self.assertRaisesRegex(ai.ProviderError, 'usage limit'):
            self.cli('claude_code', CLAUDE_SCRIPT, 'limit').stream([{'role': 'user', 'content': 'hi'}])

    def test_codex_streams_agent_message_read_only(self):
        provider = self.cli('codex', CODEX_SCRIPT)
        self.assertEqual(provider.status()['state'], 'ready')
        tokens = []
        self.assertEqual(provider.stream([{'role': 'user', 'content': 'hi'}], on_token=tokens.append), 'Hello from Codex')
        self.assertEqual(tokens, ['Hello', ' from Codex'])
        args = json.loads(Path(self.log).read_text(encoding='utf-8'))['args']
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertIn('--ephemeral', args)
        self.assertEqual(args[-1], '-')  # prompt from stdin
        providers.forget_status()
        self.assertEqual(self.cli('codex', CODEX_SCRIPT, 'signed_out').status()['state'], 'signed_out')

    def test_missing_cli_is_reported(self):
        provider = providers.for_settings({'provider': 'claude_code'})
        provider.exe = lambda: None
        self.assertEqual(provider.status()['state'], 'missing')
        with self.assertRaises(ai.NotConfigured):
            provider.stream([{'role': 'user', 'content': 'hi'}])

    def test_stopping_mid_answer(self):
        provider = self.cli('claude_code', CLAUDE_SCRIPT)
        cancel = threading.Event()
        text = provider.stream([{'role': 'user', 'content': 'hi'}], on_token=lambda t: cancel.set(), cancel=cancel)
        self.assertEqual(text, 'Blue')

    def test_catalog_lists_every_provider_in_order(self):
        entries = providers.catalog({'provider': 'ollama', 'base_url': 'http://127.0.0.1:9'})
        self.assertEqual([e['kind'] for e in entries], providers.ORDER)
        self.assertTrue(all(e['models'] == [] or e['status']['state'] == 'ready' for e in entries))
        self.assertTrue(next(e for e in entries if e['kind'] == 'anthropic')['cloud'])

    def test_transcript_for_single_prompt_clis(self):
        system, prompt = providers.transcript([{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'Q'}])
        self.assertEqual((system, prompt), ('S', 'Q'))


if __name__ == '__main__':
    unittest.main()
