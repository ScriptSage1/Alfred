"""PLACEHOLDER: the AI backend (Ollama, Claude, OpenAI-compatible servers) is not part of Alfred yet.

Same names and return shapes as the real module will have, so ui.py runs unchanged. Every
provider reports "not configured"; anything that would talk to a model raises NotConfigured.
Replace this file with the real backend later.
"""
OLLAMA_DEFAULT = 'http://127.0.0.1:11434'
ANTHROPIC_DEFAULT = 'https://api.anthropic.com'
CLAUDE_MODELS = [('claude-sonnet-5-5', 'Claude Sonnet 5.5'), ('claude-opus-5-5', 'Claude Opus 5.5')]
CLAUDE_PLAN_MODELS = [('sonnet', 'Claude Sonnet'), ('opus', 'Claude Opus'), ('haiku', 'Claude Haiku')]
NOT_READY = 'AI answers are not set up in this version of Alfred yet.'


class ProviderError(RuntimeError):
    pass


class NotConfigured(ProviderError):
    pass


def is_local_url(url):
    host = (url or '').split('//')[-1].split('/')[0].split(':')[0].lower()
    return host in ('127.0.0.1', 'localhost', '::1', '[::1]')


def keep_alive(settings):
    try:
        minutes = int(settings.get('keep_alive_minutes', 5))
    except (TypeError, ValueError):
        minutes = 5
    return f'{max(5, min(minutes, 24 * 60))}m'


class ChatProvider:
    def __init__(self, settings):
        self.settings = settings
        self.kind, self.model, self.configured, self.local = 'none', '', False, False

    def describe(self):
        return {'configured': False, 'provider': 'none', 'model': '', 'summary': 'No AI provider configured'}

    def require(self):
        raise NotConfigured(NOT_READY)

    def stream(self, messages, on_token=lambda t: None, cancel=None, web=False, on_status=lambda text: None):
        raise NotConfigured(NOT_READY)

    def list_models(self):
        raise NotConfigured(NOT_READY)


class Embedder:
    @classmethod
    def from_settings(cls, settings):
        return None


class Vision:
    @classmethod
    def from_settings(cls, settings):
        return None


def find_claude_code(settings=None):
    return None


def claude_code_status(settings=None, refresh=False):
    return {'installed': False, 'logged_in': False, 'method': None, 'path': None}


def claude_code_sign_in(settings=None):
    raise ProviderError(NOT_READY)


def effort_options(settings):
    return [], NOT_READY


def available_models(settings):
    return []


def ollama_running(base, timeout=0.8):
    return False


def ensure_ollama(base, wait=25.0):
    raise ProviderError(NOT_READY)


def warm_up(settings, progress=lambda text: None):
    return {'started': False, 'loaded': [], 'keep_alive': keep_alive(settings)}


def diagnostics(settings, probe=False):
    return {'chat': ChatProvider(settings).describe(), 'embeddings': {'enabled': False}, 'vision': {'enabled': False},
            'gpu': [], 'errors': [NOT_READY]}
