"""Chat providers: where Ask Alfred's answers come from.

Every provider has the same shape — ``describe()``, ``status()``, ``models()``, ``effort_options()``
and ``stream(messages, on_token, cancel, on_status)`` — so the rest of Alfred never cares which
one is in use:

* ``ollama``      a model running in Ollama on this PC (the default; nothing leaves the PC)
* ``claude_code`` Claude through your Claude Pro/Max plan, via the Claude Code CLI
* ``codex``       ChatGPT models through your ChatGPT Plus/Pro plan, via the Codex CLI
* ``anthropic``   the Claude API with your API key
* ``openai``      the OpenAI API with your API key
* ``compatible``  any OpenAI-compatible server (OpenRouter, Groq, LM Studio, llama.cpp, ...)

The two CLIs are coding agents, so Alfred runs them locked down: print mode, every tool off,
no MCP servers or project settings, an empty working folder, and the question on stdin.
Cloud providers only ever receive the conversation and the passages retrieved for each
question — never your files.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import ai, keys
from .console import hidden
from .ai import NotConfigured, ProviderError

CLI_TIMEOUT = 300
ORDER = ['ollama', 'claude_code', 'codex', 'anthropic', 'openai', 'compatible']
VIA = {'ollama': 'on this PC', 'claude_code': 'through your Claude plan', 'codex': 'through your ChatGPT plan',
       'anthropic': 'through the Claude API', 'openai': 'through the OpenAI API', 'compatible': 'through your custom server'}
GROUPS = {'ollama': 'On this PC', 'claude_code': 'Claude · your plan', 'codex': 'ChatGPT · your plan', 'anthropic': 'Claude API',
          'openai': 'OpenAI API', 'compatible': 'Custom server'}
ANTHROPIC_URL = 'https://api.anthropic.com'
OPENAI_URL = 'https://api.openai.com/v1'
CLAUDE_MODELS = [('claude-sonnet-5-5', 'Claude Sonnet 5.5'), ('claude-opus-5-5', 'Claude Opus 5.5'), ('claude-haiku-5-5', 'Claude Haiku 5.5'),
                 ('claude-fable-5-1', 'Claude Fable 5.1')]
CLAUDE_PLAN_MODELS = [('sonnet', 'Claude Sonnet'), ('opus', 'Claude Opus'), ('haiku', 'Claude Haiku')]
CLAUDE_EFFORTS = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High')]


def _sse(response, cancel):
    """Yield the JSON payloads of a server-sent-events stream."""
    for raw in response:
        if cancel is not None and cancel.is_set():
            return
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def _post_stream(url, payload, headers):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=dict({'Content-Type': 'application/json'}, **headers))
    try:
        return urllib.request.urlopen(request, timeout=300)
    except urllib.error.HTTPError as error:
        raise ProviderError(_http_message(error)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        raise ProviderError(f'Cannot reach {urllib.parse.urlparse(url).netloc}: {getattr(error, "reason", error)}') from None


def _get_json(url, headers, timeout=8):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise ProviderError(_http_message(error)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
        raise ProviderError(f'Cannot reach {urllib.parse.urlparse(url).netloc}: {getattr(error, "reason", error)}') from None


def _http_message(error):
    try:
        body = error.read().decode('utf-8', 'replace')
    finally:
        error.close()
    try:
        detail = json.loads(body).get('error')
        detail = detail.get('message') if isinstance(detail, dict) else detail
    except ValueError:
        detail = body[:200]
    if error.code in (401, 403):
        return f'The key was rejected ({error.code}). Check it in Settings → AI model.'
    if error.code == 429:
        return f'Rate or usage limit reached — try again shortly. {detail or ""}'.strip()
    return f'HTTP {error.code}: {detail or "request failed"}'


def transcript(messages):
    """(system prompt, one prompt string) for CLIs that take a single prompt."""
    system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
    turns = [m for m in messages if m['role'] != 'system']
    last = turns[-1]['content'] if turns else ''
    earlier = turns[:-1]
    if not earlier:
        return system, last
    history = '\n\n'.join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in earlier)
    return system, f'Conversation so far:\n\n{history}\n\nUser: {last}'


class Provider:
    kind = ''
    cloud = True

    def __init__(self, settings, model=None):
        self.settings = settings
        self.model = (model if model is not None else settings.get('model') or '').strip()

    @property
    def label(self):
        return GROUPS[self.kind]

    @property
    def via(self):
        """'through your Claude plan', 'on this PC', ..."""
        return VIA[self.kind] if self.cloud or self.kind == 'ollama' else 'on this PC'

    @property
    def configured(self):
        return bool(self.model)

    def describe(self):
        if not self.configured:
            return {'configured': False, 'provider': self.kind, 'model': '', 'summary': 'No chat model chosen'}
        where = 'on this PC' if not self.cloud else self.label
        return {'configured': True, 'provider': self.kind, 'model': self.model, 'name': self.model, 'cloud': self.cloud,
                'location': 'remote' if self.cloud else 'local', 'group': self.label, 'summary': f'{self.model} · {where}'}

    def require(self):
        if not self.configured:
            raise NotConfigured('Choose a chat model first (the model button at the top of Ask Alfred).')

    def status(self):
        """{'state': 'ready'|'signed_out'|'missing'|'no_key'|'unreachable', 'detail': str}"""
        return {'state': 'ready', 'detail': ''}

    def models(self):
        """[(model id, label)] this provider offers."""
        return []

    def effort_options(self):
        return [], f'{self.model} answers directly.'

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        raise NotImplementedError


# ------------------------------------------------------------------ ollama
class Ollama(Provider):
    kind = 'ollama'

    def __init__(self, settings, model=None):
        super().__init__(settings, model)
        self.model = self.model.removesuffix(':latest')
        self.base = ai._ollama_base(settings.get('base_url'))

    @property
    def cloud(self):
        return not ai.is_local_url(self.base)

    def status(self):
        if ai.ollama_running(self.base, 1.0):
            return {'state': 'ready', 'detail': 'Running'}
        if ai.is_local_url(self.base) and ai.find_ollama():
            return {'state': 'ready', 'detail': 'Starts when needed'}
        return {'state': 'missing', 'detail': 'Ollama is not installed (ollama.com)'}

    def models(self):
        return [(m['model'], m['model']) for m in ai.available_models(self.settings)]

    def effort_options(self):
        if not self.configured:
            return [], 'Choose a chat model first.'
        if 'thinking' not in ai.model_capabilities(self.base, self.model):
            return [], f'{self.model} answers directly (it has no thinking mode).'
        if 'gpt-oss' in self.model:
            return [('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], 'How long the model thinks before answering.'
        return [('off', 'Off'), ('on', 'On')], 'Thinking first gives better answers to hard questions, but takes longer.'

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        self.require()
        effort = (self.settings.get('effort') or 'off').lower()
        # Room for the passages Ask Alfred sends (about 6k tokens), the chat so far and the answer; Ollama's default is 4k.
        context = max(2048, min(int(self.settings.get('ollama_context') or 10240), 131072))
        payload = {'model': self.model, 'messages': messages, 'stream': True, 'keep_alive': ai.keep_alive(self.settings),
                   'options': dict(ai._options(self.settings.get('device', 'auto')), temperature=self.settings.get('temperature', 0.2),
                                   num_ctx=context)}
        if 'thinking' in ai.model_capabilities(self.base, self.model):
            payload['think'] = effort if 'gpt-oss' in self.model else effort != 'off'
        parts = []
        with _post_stream(self.base + '/api/chat', payload, {}) as response:
            for raw in response:
                if cancel is not None and cancel.is_set():
                    break
                event = json.loads(raw.decode('utf-8', 'replace') or '{}')
                if event.get('error'):
                    raise ProviderError(event['error'])
                message = event.get('message') or {}
                if message.get('thinking') and not parts:
                    on_status('Thinking…')
                if message.get('content'):
                    parts.append(message['content'])
                    on_token(message['content'])
                if event.get('done'):
                    break
        return ''.join(parts)


# --------------------------------------------------------- openai-style API
class OpenAICompatible(Provider):
    """The OpenAI chat-completions API, which most other providers and local servers also speak."""
    kind = 'compatible'
    key_name = 'compatible'

    @property
    def base(self):
        return (self.settings.get('compatible_base_url') or '').strip().rstrip('/')

    @property
    def cloud(self):
        return not ai.is_local_url(self.base)

    def headers(self):
        key = keys.get(self.key_name)
        return {'Authorization': f'Bearer {key}'} if key else {}

    def status(self):
        if not self.base:
            return {'state': 'no_key', 'detail': 'Add the server address'}
        return {'state': 'ready', 'detail': self.base}

    def models(self):
        if self.status()['state'] != 'ready':
            return []
        try:
            data = _get_json(self.base + '/models', self.headers()).get('data', [])
        except ProviderError:
            return []
        return sorted((m['id'], m['id']) for m in data if m.get('id'))

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        self.require()
        if self.status()['state'] != 'ready':
            raise NotConfigured(f'{self.label} isn’t set up — add it in Settings → AI model.')
        payload = {'model': self.model, 'messages': messages, 'stream': True}
        parts = []
        with _post_stream(self.base + '/chat/completions', payload, self.headers()) as response:
            for event in _sse(response, cancel):
                if event.get('error'):
                    raise ProviderError(str(event['error'].get('message') if isinstance(event['error'], dict) else event['error']))
                for choice in event.get('choices') or []:
                    text = (choice.get('delta') or {}).get('content')
                    if text:
                        parts.append(text)
                        on_token(text)
        return ''.join(parts)


class OpenAI(OpenAICompatible):
    kind = 'openai'
    key_name = 'openai'
    cloud = True

    @property
    def base(self):
        return OPENAI_URL

    def status(self):
        return {'state': 'ready', 'detail': 'Key saved'} if keys.has(self.key_name) else {'state': 'no_key', 'detail': 'Add an API key'}

    def models(self):
        if not keys.has(self.key_name):
            return []
        try:
            data = _get_json(self.base + '/models', self.headers()).get('data', [])
        except ProviderError:
            return []
        chat = [m['id'] for m in data if m.get('id', '').startswith(('gpt-', 'o')) and not any(
            word in m['id'] for word in ('audio', 'realtime', 'transcribe', 'tts', 'image', 'embedding', 'search', 'moderation'))]
        return [(m, m) for m in sorted(chat, reverse=True)]


# --------------------------------------------------------------- anthropic
class Anthropic(Provider):
    kind = 'anthropic'

    def headers(self):
        return {'x-api-key': keys.get('anthropic'), 'anthropic-version': '2023-06-01'}

    def status(self):
        return {'state': 'ready', 'detail': 'Key saved'} if keys.has('anthropic') else {'state': 'no_key', 'detail': 'Add an API key'}

    def models(self):
        if not keys.has('anthropic'):
            return []
        try:
            data = _get_json(ANTHROPIC_URL + '/v1/models?limit=50', self.headers()).get('data', [])
            found = [(m['id'], m.get('display_name') or m['id']) for m in data if m.get('id')]
            return found or CLAUDE_MODELS
        except ProviderError:
            return CLAUDE_MODELS

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        self.require()
        if not keys.has('anthropic'):
            raise NotConfigured('Add your Anthropic API key in Settings → AI model.')
        system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
        payload = {'model': self.model, 'max_tokens': 4096, 'stream': True,
                   'messages': [{'role': m['role'], 'content': m['content']} for m in messages if m['role'] in ('user', 'assistant')]}
        if system:
            payload['system'] = system
        parts = []
        with _post_stream(ANTHROPIC_URL + '/v1/messages', payload, self.headers()) as response:
            for event in _sse(response, cancel):
                if event.get('type') == 'error':
                    raise ProviderError((event.get('error') or {}).get('message') or 'The Claude API returned an error')
                delta = event.get('delta') or {}
                if event.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                    parts.append(delta['text'])
                    on_token(delta['text'])
        return ''.join(parts)


# ---------------------------------------------------------------- the CLIs
def _newest(paths):
    found = [p for p in paths if p and Path(p).is_file()]
    return str(max(found, key=lambda p: Path(p).stat().st_mtime)) if found else None


def find_claude():
    """Claude Code: on PATH, installed natively or by npm, or bundled with the Claude desktop app."""
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    bundled = list((local / 'Claude-Data').glob('*/claude-code/*/*/claude.exe')) if (local / 'Claude-Data').is_dir() else []
    return _newest([shutil.which('claude'), str(Path.home() / '.local' / 'bin' / 'claude.exe'),
                    str(Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude.cmd')] + [str(p) for p in bundled])


def find_codex():
    """Codex: on PATH, installed by npm, or bundled with the Codex desktop app."""
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    bundled = list((local / 'OpenAI' / 'Codex' / 'bin').glob('*/codex.exe')) if (local / 'OpenAI' / 'Codex' / 'bin').is_dir() else []
    return _newest([shutil.which('codex'), str(Path(os.environ.get('APPDATA', '')) / 'npm' / 'codex.cmd')] + [str(p) for p in bundled])


_status_cache = {}


def _cached(key, compute, seconds=60):
    hit = _status_cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = compute()
    _status_cache[key] = (time.time(), value)
    return value


def forget_status():
    _status_cache.clear()


class CLIProvider(Provider):
    """Shared plumbing: run the CLI hidden in an empty folder, feed the prompt on stdin, read JSON lines."""
    install_hint = ''
    login_args = []

    def exe(self):
        raise NotImplementedError

    def workdir(self):
        from . import config
        folder = config.data_home() / 'cli-runs'
        folder.mkdir(exist_ok=True)
        return folder

    def env(self):
        return dict(os.environ)

    def sign_in(self):
        """Open a console window running the CLI's own sign-in; the user finishes it there."""
        exe = self.exe()
        if not exe:
            raise ProviderError(self.install_hint)
        forget_status()
        subprocess.Popen(['cmd', '/c', 'start', 'Sign in for Alfred', '/wait', 'cmd', '/k', exe] + self.login_args, cwd=str(self.workdir()),
                         env=self.env())

    def run(self, args, prompt, handle, cancel):
        """Run the CLI with ``prompt`` on stdin; ``handle(event)`` gets each JSON line. Returns the exit code."""
        with tempfile.TemporaryDirectory(dir=self.workdir()) as folder:
            process = subprocess.Popen([self.exe()] + args, cwd=folder, env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, **hidden())
            errors = []
            reader = threading.Thread(target=lambda: errors.append(process.stderr.read().decode('utf-8', 'replace')), daemon=True)
            reader.start()
            watchdog = threading.Timer(CLI_TIMEOUT, process.kill)
            watchdog.start()
            try:
                process.stdin.write(prompt.encode('utf-8'))
                process.stdin.close()
                for raw in process.stdout:
                    if cancel is not None and cancel.is_set():
                        process.kill()
                        break
                    line = raw.decode('utf-8', 'replace').strip()
                    if line.startswith('{'):
                        try:
                            handle(json.loads(line))
                        except ValueError:
                            continue
                code = process.wait()
            finally:
                watchdog.cancel()
                if process.poll() is None:
                    process.kill()
            reader.join(2)
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            self.stderr = ''.join(errors).strip()
            return code


class ClaudeCode(CLIProvider):
    kind = 'claude_code'
    install_hint = 'Claude Code wasn’t found. Install the Claude desktop app, or run: npm i -g @anthropic-ai/claude-code'
    login_args = ['auth', 'login']

    def __init__(self, settings, model=None):
        super().__init__(settings, model)
        self.model = self.model or 'sonnet'

    def exe(self):
        return find_claude()

    def env(self):
        env = dict(os.environ)
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
            env.pop(name, None)  # use the Claude plan, never silently an API key
        return env

    def account(self):
        exe = self.exe()
        if not exe:
            return {'installed': False}

        def check():
            try:
                out = subprocess.run([exe, 'auth', 'status'], capture_output=True, text=True, timeout=30, env=self.env(),
                                     encoding='utf-8', errors='replace', **hidden())
                info = json.loads(out.stdout[out.stdout.find('{'):] or '{}')
            except (OSError, ValueError, subprocess.TimeoutExpired):
                info = {}
            return {'installed': True, 'logged_in': bool(info.get('loggedIn')), 'plan': info.get('subscriptionType'),
                    'method': info.get('authMethod'), 'email': info.get('email')}
        return _cached(('claude', exe), check)

    def status(self):
        account = self.account()
        if not account['installed']:
            return {'state': 'missing', 'detail': 'Claude Code not found'}
        if not account['logged_in']:
            return {'state': 'signed_out', 'detail': 'Not signed in'}
        plan = (account.get('plan') or '').title()
        return {'state': 'ready', 'detail': f"Signed in{' · ' + plan if plan else ''}" + (f" · {account['email']}" if account.get('email') else '')}

    def models(self):
        return CLAUDE_PLAN_MODELS if self.status()['state'] == 'ready' else []

    def effort_options(self):
        return CLAUDE_EFFORTS, 'How hard Claude thinks before answering (uses more of your plan’s limit when higher).'

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        self.require()
        if not self.exe():
            raise NotConfigured(self.install_hint)
        system, prompt = transcript(messages)
        args = ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--model', self.model,
                '--tools', '', '--strict-mcp-config', '--setting-sources', '', '--no-session-persistence']
        effort = self.settings.get('effort')
        if effort in dict(CLAUDE_EFFORTS):
            args += ['--effort', effort]
        if system:
            args += ['--system-prompt', system]
        parts, result = [], {}

        def handle(event):
            if event.get('type') == 'stream_event':
                inner = event.get('event') or {}
                delta = inner.get('delta') or {}
                if inner.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                    parts.append(delta['text'])
                    on_token(delta['text'])
                elif inner.get('type') == 'content_block_start' and (inner.get('content_block') or {}).get('type') == 'thinking':
                    on_status('Thinking…')
            elif event.get('type') == 'result':
                result.update(event)
        code = self.run(args, prompt, handle, cancel)
        if cancel is not None and cancel.is_set():
            return ''.join(parts)
        if result.get('is_error') or (code and not parts):
            message = str(result.get('result') or self.stderr or f'Claude Code exited with code {code}')
            if 'login' in message.lower() or 'logged in' in message.lower() or 'authenticate' in message.lower():
                forget_status()
                raise NotConfigured('Claude Code isn’t signed in. Settings → AI model → Sign in with Claude.')
            if 'limit' in message.lower():
                raise ProviderError(f'Your Claude plan’s usage limit was reached. {message}')
            raise ProviderError(message[:400])
        text = ''.join(parts) or str(result.get('result') or '')
        if text and not parts:
            on_token(text)
        return text


class Codex(CLIProvider):
    kind = 'codex'
    install_hint = 'Codex wasn’t found. Install the Codex app, or run: npm i -g @openai/codex'
    login_args = ['login']

    def exe(self):
        return find_codex()

    def account(self):
        exe = self.exe()
        if not exe:
            return {'installed': False}

        def check():
            try:
                out = subprocess.run([exe, 'login', 'status'], capture_output=True, text=True, timeout=30, encoding='utf-8',
                                     errors='replace', **hidden())
                text = (out.stdout + out.stderr).strip()
            except (OSError, subprocess.TimeoutExpired):
                text = ''
            logged_in = 'logged in' in text.lower() and 'not logged in' not in text.lower()
            return {'installed': True, 'logged_in': logged_in, 'detail': text.splitlines()[0] if text else ''}
        return _cached(('codex', exe), check)

    def status(self):
        account = self.account()
        if not account['installed']:
            return {'state': 'missing', 'detail': 'Codex not found'}
        if not account['logged_in']:
            return {'state': 'signed_out', 'detail': 'Not signed in'}
        return {'state': 'ready', 'detail': account.get('detail') or 'Signed in'}

    @property
    def configured(self):
        return True  # an empty model means "Codex's default model"

    def describe(self):
        info = super().describe()
        if not self.model:
            info.update(model='Codex default', name='Codex default', summary=f'Codex default model · {self.label}')
        return info

    def models(self):
        if self.status()['state'] != 'ready':
            return []
        found = [('', 'Codex default')]
        cache = Path.home() / '.codex' / 'models_cache.json'
        try:
            data = json.loads(cache.read_text(encoding='utf-8'))
            items = data.get('models', data) if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                slug = item.get('slug') or item.get('id') if isinstance(item, dict) else item
                if isinstance(slug, str) and slug not in [m for m, _ in found]:
                    found.append((slug, slug))
        except (OSError, ValueError, AttributeError):
            pass
        return found

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        if not self.exe():
            raise NotConfigured(self.install_hint)
        system, prompt = transcript(messages)
        if system:
            prompt = f'{system}\n\n{prompt}'
        args = ['exec', '--json', '--skip-git-repo-check', '--sandbox', 'read-only', '--ephemeral', '--color', 'never']
        if self.model:
            args += ['-m', self.model]
        args.append('-')  # the prompt comes from stdin
        parts, failure = [], []

        def handle(event):
            kind = event.get('type', '')
            item = event.get('item') or {}
            if kind in ('item.completed', 'item.updated') and item.get('type') == 'agent_message':
                text = item.get('text') or ''
                sent = ''.join(parts)
                if text.startswith(sent) and len(text) > len(sent):
                    parts.append(text[len(sent):])
                    on_token(text[len(sent):])
            elif kind == 'item.started' and item.get('type') == 'reasoning':
                on_status('Thinking…')
            elif kind in ('turn.failed', 'error'):
                failure.append(str((event.get('error') or {}).get('message') if isinstance(event.get('error'), dict)
                                   else event.get('message') or event.get('error') or 'Codex failed'))
        code = self.run(args, prompt, handle, cancel)
        if cancel is not None and cancel.is_set():
            return ''.join(parts)
        if failure or (code and not parts):
            message = failure[0] if failure else (self.stderr or f'Codex exited with code {code}')
            if 'login' in message.lower() or 'not logged in' in message.lower() or '401' in message:
                forget_status()
                raise NotConfigured('Codex isn’t signed in. Settings → AI model → Sign in with ChatGPT.')
            if 'limit' in message.lower():
                raise ProviderError(f'Your ChatGPT plan’s usage limit was reached. {message}')
            raise ProviderError(message[:400])
        return ''.join(parts)


KINDS = {'ollama': Ollama, 'claude_code': ClaudeCode, 'codex': Codex, 'anthropic': Anthropic, 'openai': OpenAI,
         'compatible': OpenAICompatible}


def for_settings(settings, kind=None, model=None):
    """The provider (and model) Ask Alfred should use."""
    kind = kind or settings.get('provider') or 'ollama'
    return KINDS.get(kind, Ollama)(settings, model)


def catalog(settings):
    """Every provider with its status and models, in menu order:
    [{'kind', 'group', 'cloud', 'status', 'models': [(id, label)]}]"""
    entries = []
    for kind in ORDER:
        provider = KINDS[kind](settings, '')
        try:
            status = provider.status()
            models = provider.models() if status['state'] == 'ready' else []
        except Exception as error:  # one broken provider never hides the others
            status, models = {'state': 'unreachable', 'detail': str(error)[:120]}, []
        entries.append({'kind': kind, 'group': GROUPS[kind], 'cloud': provider.cloud, 'status': status, 'models': models})
    return entries
