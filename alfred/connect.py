"""Add Alfred's MCP server to the AI tools on this PC (Settings → AI tools), whichever tool and model they use.

Every tool runs the same server (``server_spec``). Tools with an ``mcp add`` command (Claude Code, Codex) are changed
through that command, given as an argument list, so no shell quoting is involved (Windows PowerShell drops a bare
``--``). Editors and desktop apps keep their servers in a JSON file; Alfred changes only its own ``alfred`` entry
there and refuses files it can't parse (JSON with comments) rather than rewrite them. Any other tool gets the same
program, arguments and environment to paste (``other_tool_json``).
"""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from .console import hidden

NAME = 'alfred'
CLI_TIMEOUT = 60


class ConnectError(Exception):
    pass


def server_spec():
    """What every AI tool runs: the packaged Alfred.exe, or this install's Python with the install folder on its path
    (Alfred isn't installed into its .venv, and the tools start the server from their own working folder)."""
    exe = sys.executable
    if getattr(sys, 'frozen', False):
        return {'command': exe, 'args': ['--mcp'], 'env': {}}
    quiet = Path(exe).with_name('pythonw.exe')  # no console window for tools that don't hide one
    folder = str(Path(__file__).resolve().parent.parent)
    return {'command': str(quiet if quiet.is_file() else exe), 'args': ['-m', 'alfred', '--mcp'], 'env': {'PYTHONPATH': folder}}


def other_tool_json():
    """The entry for any MCP client that takes the common {"mcpServers": {...}} format."""
    return json.dumps({'mcpServers': {NAME: server_spec()}}, indent=2)


def _same(entry, spec):
    return (isinstance(entry, dict) and entry.get('command') == spec['command'] and list(entry.get('args') or []) == spec['args']
            and dict(entry.get('env') or {}) == spec['env'])


def _run(args, cwd=None):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=CLI_TIMEOUT,
                             stdin=subprocess.DEVNULL, **hidden())
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConnectError(f'{Path(args[0]).name} did not run: {error}') from error
    return out.returncode, (out.stderr or out.stdout or '').strip()


class Tool:
    id = name = ''

    def found(self):
        raise NotImplementedError

    def entry(self):
        """Alfred's current entry in this tool, or None."""
        raise NotImplementedError

    def status(self):
        entry = self.entry()
        if entry is None:
            return {'state': 'not_added', 'detail': 'Not connected'}
        if _same(entry, server_spec()):
            return {'state': 'connected', 'detail': 'Connected'}
        return {'state': 'outdated', 'detail': 'Added, but for another Alfred or an older setup: update it'}

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError


# ------------------------------------------------------------------ tools with an `mcp add` command
class ClaudeCode(Tool):
    id, name = 'claude_code', 'Claude Code'
    config = Path.home() / '.claude.json'

    def exe(self):
        from .providers import find_claude
        return find_claude()

    def found(self):
        return bool(self.exe())

    def _config(self):
        try:
            return json.loads(self.config.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def entry(self):
        return (self._config().get('mcpServers') or {}).get(NAME)

    def folder_entries(self):
        """Folders with their own (local scope) alfred entry: there it shadows the one for every folder."""
        projects = self._config().get('projects') or {}
        return [folder for folder, info in projects.items() if NAME in ((info or {}).get('mcpServers') or {})]

    def status(self):
        status = super().status()
        stale = self.folder_entries()
        if stale and status['state'] == 'connected':
            return {'state': 'outdated', 'detail': f"Connected, but {len(stale)} folder{'s' if len(stale) > 1 else ''} still "
                                                   'use an older entry: update it'}
        return status

    def _remove_everywhere(self, exe):
        _run([exe, 'mcp', 'remove', NAME, '-s', 'user'])
        for folder in self.folder_entries():
            if Path(folder).is_dir():
                _run([exe, 'mcp', 'remove', NAME, '-s', 'local'], cwd=folder)

    def connect(self):
        exe = self.exe()
        if not exe:
            raise ConnectError('Claude Code wasn’t found')
        self._remove_everywhere(exe)
        spec = server_spec()
        env = [part for key, value in spec['env'].items() for part in ('-e', f'{key}={value}')]
        code, text = _run([exe, 'mcp', 'add', NAME, '-s', 'user'] + env + ['--', spec['command']] + spec['args'])
        if code:
            raise ConnectError(text or f'claude mcp add failed ({code})')

    def disconnect(self):
        exe = self.exe()
        if exe:
            self._remove_everywhere(exe)


class Codex(Tool):
    id, name = 'codex', 'Codex'
    config = Path.home() / '.codex' / 'config.toml'

    def exe(self):
        from .providers import find_codex
        return find_codex()

    def found(self):
        return bool(self.exe())

    def entry(self):
        try:
            with open(self.config, 'rb') as file:
                return (tomllib.load(file).get('mcp_servers') or {}).get(NAME)
        except (OSError, tomllib.TOMLDecodeError):
            return None

    def connect(self):
        exe = self.exe()
        if not exe:
            raise ConnectError('Codex wasn’t found')
        if self.entry() is not None:
            _run([exe, 'mcp', 'remove', NAME])
        spec = server_spec()
        env = [part for key, value in spec['env'].items() for part in ('--env', f'{key}={value}')]
        code, text = _run([exe, 'mcp', 'add', NAME] + env + ['--', spec['command']] + spec['args'])
        if code:
            raise ConnectError(text or f'codex mcp add failed ({code})')

    def disconnect(self):
        exe = self.exe()
        if exe and self.entry() is not None:
            _run([exe, 'mcp', 'remove', NAME])


# ------------------------------------------------------------------ tools that keep servers in a JSON file
class JsonTool(Tool):
    key = 'mcpServers'
    extra = {}  # fields this tool wants besides command, args and env

    def path(self):
        raise NotImplementedError

    def home(self):
        """The tool's own settings folder: the tool counts as installed when it exists."""
        return self.path().parent

    def found(self):
        return self.home().is_dir()

    def _load(self):
        path = self.path()
        if not path.is_file() or not path.read_text(encoding='utf-8-sig').strip():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except ValueError as error:
            raise ConnectError(f'{path.name} isn’t plain JSON (comments?), so Alfred leaves it alone. '
                               'Add the entry by hand: Copy, under Any other tool.') from error
        if not isinstance(data, dict):
            raise ConnectError(f'{path.name} isn’t a JSON object; add the entry by hand')
        return data

    def _save(self, data):
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_name(path.name + '.before-alfred')
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
        temp = path.with_name(path.name + '.alfred-tmp')
        temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, path)

    def entry(self):
        try:
            servers = self._load().get(self.key)
        except ConnectError:
            return None
        return servers.get(NAME) if isinstance(servers, dict) else None

    def connect(self):
        data = self._load()
        servers = data.get(self.key)
        if not isinstance(servers, dict):
            servers = data[self.key] = {}
        servers[NAME] = dict(self.extra, **server_spec())
        self._save(data)

    def disconnect(self):
        data = self._load()
        if NAME in (data.get(self.key) or {}):
            del data[self.key][NAME]
            self._save(data)


def _appdata():
    return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')


class ClaudeDesktop(JsonTool):
    id, name = 'claude_desktop', 'Claude Desktop'

    def _packaged(self):
        """The Microsoft Store build keeps its own copy of %APPDATA% under its package folder."""
        packages = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'Packages'
        return [p / 'LocalCache' / 'Roaming' / 'Claude' for p in packages.glob('Claude_*')] if packages.is_dir() else []

    def path(self):
        for folder in self._packaged():
            if (folder / 'claude_desktop_config.json').is_file():
                return folder / 'claude_desktop_config.json'
        return _appdata() / 'Claude' / 'claude_desktop_config.json'

    def found(self):
        return (_appdata() / 'Claude').is_dir() or any(folder.parent.parent.parent.is_dir() for folder in self._packaged())


class Cursor(JsonTool):
    id, name = 'cursor', 'Cursor'

    def path(self):
        return Path.home() / '.cursor' / 'mcp.json'


class VSCode(JsonTool):
    id, name = 'vscode', 'VS Code (Copilot)'
    key, extra = 'servers', {'type': 'stdio'}

    def path(self):
        return _appdata() / 'Code' / 'User' / 'mcp.json'


class Windsurf(JsonTool):
    id, name = 'windsurf', 'Windsurf'

    def path(self):
        return Path.home() / '.codeium' / 'windsurf' / 'mcp_config.json'


class GeminiCLI(JsonTool):
    id, name = 'gemini', 'Gemini CLI'

    def path(self):
        return Path.home() / '.gemini' / 'settings.json'

    def found(self):
        return self.path().is_file()


TOOLS = [ClaudeCode(), Codex(), ClaudeDesktop(), Cursor(), VSCode(), Windsurf(), GeminiCLI()]


def found_tools():
    return [tool for tool in TOOLS if tool.found()]


def tool(tool_id):
    return next(t for t in TOOLS if t.id == tool_id)
