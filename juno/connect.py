"""Add Juno's MCP server to the AI tools on this PC (Settings → AI tools), whichever tool and model they use.

Every tool runs the same server (``server_spec``). Tools with an ``mcp add`` command (Claude Code, Codex) are changed
through that command, given as an argument list, so no shell quoting is involved (Windows PowerShell drops a bare
``--``). Editors and desktop apps keep their servers in a JSON file; Juno changes only its own ``juno`` entry
there, keeps the original once (``<file>.before-juno``) and refuses files it can't parse (JSON with comments)
rather than rewrite them. Any other tool gets the same entry to paste (``other_tool_json``).

After connecting, ``check_server`` starts the server exactly as the tool will (its command, arguments and environment,
from an unrelated working folder) and runs the MCP handshake, so "Connected" means the tool can really start it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .console import hidden

NAME = 'juno'
OLD_NAME = 'alfred'  # the entry's name before Juno was renamed (November 2026); move_from_alfred replaces it
CLI_TIMEOUT = 60
CHECK_TIMEOUT = 45  # a cold start imports numpy and opens the index
PASSED_ENV = ('JUNO_HOME', 'JUNO_MODELS_DIR', 'JUNO_LLAMA_DIR')  # the tools' server uses this Juno's data


class ConnectError(Exception):
    pass


def running_programs():
    """Full paths (lower case) of the programs running on this PC that this user can see; empty when it can't be read
    (another OS, or Windows refused)."""
    if os.name != 'nt':
        return []
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        ids = (wintypes.DWORD * 4096)()
        used = wintypes.DWORD()
        if not kernel.K32EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(used)):
            return []
        kernel.OpenProcess.restype = wintypes.HANDLE
        paths = []
        for pid in ids[:used.value // ctypes.sizeof(wintypes.DWORD)]:
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                buffer, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if kernel.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buffer, ctypes.byref(size)):
                    paths.append(buffer.value.lower())
            finally:
                kernel.CloseHandle(wintypes.HANDLE(handle))
        return paths
    except Exception:
        return []


def server_spec():
    """What every AI tool runs: the packaged Juno.exe, or this install's Python with the install folder on its path
    (Juno isn't installed into its .venv, and the tools start the server from their own working folder). Data and
    model folders this Juno was started with are passed on, so the server reads the same index."""
    env = {name: os.environ[name] for name in PASSED_ENV if os.environ.get(name)}
    exe = sys.executable
    if getattr(sys, 'frozen', False):
        return {'command': exe, 'args': ['--mcp'], 'env': env}
    quiet = Path(exe).with_name('pythonw.exe')  # no console window for tools that don't hide one
    folder = str(Path(__file__).resolve().parent.parent)
    return {'command': str(quiet if quiet.is_file() else exe), 'args': ['-m', 'juno', '--mcp'], 'env': dict(env, PYTHONPATH=folder)}


def other_tool_json():
    """The entry for any MCP client that takes the common {"mcpServers": {...}} format."""
    return json.dumps({'mcpServers': {NAME: server_spec()}}, indent=2)


def _same(entry, spec):
    return (isinstance(entry, dict) and entry.get('command') == spec['command'] and list(entry.get('args') or []) == spec['args']
            and {str(k): str(v) for k, v in (entry.get('env') or {}).items()} == spec['env'])


def _run(args, cwd=None):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=CLI_TIMEOUT,
                             stdin=subprocess.DEVNULL, **hidden())
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConnectError(f'{Path(args[0]).name} did not run: {error}') from error
    return out.returncode, (out.stderr or out.stdout or '').strip()


def _kill_tree(process):
    """Stop a process and what it started: a venv's python.exe is a launcher that runs the real interpreter as a child."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True, timeout=30, **hidden())
    process.kill()
    try:
        process.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def check_server(entry, timeout=CHECK_TIMEOUT):
    """Start the server as an AI tool would (``entry``: command, args, env) and run the MCP handshake. Raises
    ConnectError with the reason when it doesn't answer as Juno."""
    command = entry.get('command')
    if not command or not Path(command).is_file():
        raise ConnectError(f'The program it runs is missing: {command}')
    env = dict(os.environ, **{str(k): str(v) for k, v in (entry.get('env') or {}).items()})
    hello = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'juno-setup-check', 'version': '1'}}}
    listing = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
    # Not the install folder: tools start it from their own. A process left behind may still hold the folder, so its
    # clean-up never fails the check.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        try:
            process = subprocess.Popen([command] + [str(a) for a in entry.get('args') or []], cwd=folder, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden())
        except OSError as error:
            raise ConnectError(f'The server did not start: {error}') from error
        try:
            out, err = process.communicate((json.dumps(hello) + '\n' + json.dumps(listing) + '\n').encode('utf-8'), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            raise ConnectError(f'The server did not answer within {timeout} s') from None
    replies = {}
    for line in out.decode('utf-8', 'replace').splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and 'id' in message:
            replies[message['id']] = message
    info = ((replies.get(1) or {}).get('result') or {}).get('serverInfo') or {}
    tools = [t.get('name') for t in ((replies.get(2) or {}).get('result') or {}).get('tools') or []]
    if info.get('name') != NAME or 'search_files' not in tools:
        reason = err.decode('utf-8', 'replace').strip().splitlines()
        raise ConnectError('The server did not start: ' + (reason[-1] if reason else f'exit code {process.returncode}'))


class Tool:
    id = name = ''

    def found(self):
        raise NotImplementedError

    def running(self, programs=None):
        """True when the tool is open and could write back its own copy of its settings over Juno's change, so it has to
        be quit and reopened. Only tools known to do that say so. ``programs``: running_programs(), read once."""
        return False

    def entry(self, name=NAME):
        """Juno's current entry in this tool ({command, args, env}), or None. ``name``: OLD_NAME for Alfred's."""
        raise NotImplementedError

    def status(self):
        entry = self.entry()
        if entry is None:
            return {'state': 'not_added', 'detail': 'Not connected'}
        if _same(entry, server_spec()):
            return {'state': 'connected', 'detail': 'Connected'}
        return {'state': 'outdated', 'detail': 'Added, but for another Juno or an older setup: update it'}

    def add(self):
        raise NotImplementedError

    def connect(self):
        """Add (or replace) Juno's entry, then check that the tool can start the server as it is now configured."""
        self.add()
        entry = self.entry()
        if entry is None:
            raise ConnectError(f'{self.name} did not keep the entry')
        check_server(entry)

    def disconnect(self, name=NAME):
        raise NotImplementedError


# ------------------------------------------------------------------ tools with an `mcp add` command
class ClaudeCode(Tool):
    """Claude Code keeps servers for every folder (user scope) in ~/.claude.json ($CLAUDE_CONFIG_DIR/.claude.json),
    and per-folder (local scope) ones in the same file under "projects"; a per-folder entry wins in its folder."""
    id, name = 'claude_code', 'Claude Code'

    def exe(self):
        from .providers import find_claude
        return find_claude()

    def found(self):
        return bool(self.exe())

    def config(self):
        folder = os.environ.get('CLAUDE_CONFIG_DIR')
        return Path(folder) / '.claude.json' if folder else Path.home() / '.claude.json'

    def _config(self):
        try:
            data = json.loads(self.config().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def entry(self, name=NAME):
        if name == OLD_NAME and self.folder_entries(name):
            return {}  # only per-folder entries left: still something to move over
        return (self._config().get('mcpServers') or {}).get(name)

    def folder_entries(self, name=NAME):
        """Folders with their own (local scope) juno entry: there it shadows the one for every folder."""
        projects = self._config().get('projects') or {}
        return [folder for folder, info in projects.items() if name in ((info or {}).get('mcpServers') or {})]

    def status(self):
        status = super().status()
        stale = self.folder_entries()
        if stale and status['state'] == 'connected':
            return {'state': 'outdated', 'detail': f"Connected, but {len(stale)} folder{'s' if len(stale) > 1 else ''} still "
                                                   'use an older entry: update it'}
        return status

    def _remove_everywhere(self, exe, name=NAME):
        _run([exe, 'mcp', 'remove', name, '-s', 'user'])
        for folder in self.folder_entries(name):
            if Path(folder).is_dir():
                _run([exe, 'mcp', 'remove', name, '-s', 'local'], cwd=folder)

    def add(self):
        exe = self.exe()
        if not exe:
            raise ConnectError('Claude Code wasn’t found')
        self._remove_everywhere(exe)
        spec = server_spec()
        env = [part for key, value in spec['env'].items() for part in ('-e', f'{key}={value}')]
        code, text = _run([exe, 'mcp', 'add', NAME, '-s', 'user'] + env + ['--', spec['command']] + spec['args'])
        if code:
            raise ConnectError(text or f'claude mcp add failed ({code})')

    def disconnect(self, name=NAME):
        exe = self.exe()
        if exe:
            self._remove_everywhere(exe, name)


class Codex(Tool):
    """Codex keeps servers in ~/.codex/config.toml ($CODEX_HOME/config.toml) under [mcp_servers.<name>]."""
    id, name = 'codex', 'Codex'

    def exe(self):
        from .providers import find_codex
        return find_codex()

    def found(self):
        return bool(self.exe())

    def config(self):
        folder = os.environ.get('CODEX_HOME')
        return (Path(folder) if folder else Path.home() / '.codex') / 'config.toml'

    def entry(self, name=NAME):
        try:
            with open(self.config(), 'rb') as file:
                return (tomllib.load(file).get('mcp_servers') or {}).get(name)
        except (OSError, tomllib.TOMLDecodeError):
            return None

    def add(self):
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

    def disconnect(self, name=NAME):
        exe = self.exe()
        if exe and self.entry(name) is not None:
            _run([exe, 'mcp', 'remove', name])


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
        try:
            text = path.read_text(encoding='utf-8-sig') if path.is_file() else ''
        except OSError as error:
            raise ConnectError(f'Couldn’t read {path.name}: {error}') from error
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ConnectError(f'{path.name} isn’t plain JSON (comments?), so Juno leaves it alone. '
                               'Add the entry by hand: Copy, under Any other tool.') from error
        if not isinstance(data, dict):
            raise ConnectError(f'{path.name} isn’t a JSON object; add the entry by hand')
        return data

    def _save(self, data):
        path = self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                backup = path.with_name(path.name + '.before-juno')
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
            temp = path.with_name(path.name + '.juno-tmp')
            temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
            os.replace(temp, path)
        except OSError as error:
            raise ConnectError(f'Couldn’t write {path.name}: {error}') from error

    def entry(self, name=NAME):
        try:
            servers = self._load().get(self.key)
        except ConnectError:
            return None
        return servers.get(name) if isinstance(servers, dict) else None

    def add(self):
        data = self._load()
        servers = data.get(self.key)
        if not isinstance(servers, dict):
            servers = data[self.key] = {}
        servers[NAME] = dict(self.extra, **server_spec())
        self._save(data)

    def disconnect(self, name=NAME):
        data = self._load()
        if name in (data.get(self.key) or {}):
            del data[self.key][name]
            self._save(data)


def _appdata():
    return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')


def _localappdata():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')


class ClaudeDesktop(JsonTool):
    """Claude Desktop reads claude_desktop_config.json from its data folder. Newer builds keep that in
    %LOCALAPPDATA%\\Claude-Data\\<package>\\ and leave a ``.user-data-left.*`` marker in the folders they moved out of
    (%APPDATA%\\Claude, the Store package's LocalCache copy); older ones use %APPDATA%\\Claude."""
    id, name = 'claude_desktop', 'Claude Desktop'
    FILE = 'claude_desktop_config.json'

    def _folders(self):
        """Candidate data folders, most likely first; folders the app has moved out of are left out."""
        data = _localappdata() / 'Claude-Data'
        moved = [p for p in data.iterdir() if p.is_dir()] if data.is_dir() else []
        moved.sort(key=lambda p: (p / self.FILE).stat().st_mtime if (p / self.FILE).is_file() else 0, reverse=True)
        packages = _localappdata() / 'Packages'
        packaged = [p / 'LocalCache' / 'Roaming' / 'Claude' for p in packages.glob('Claude_*')] if packages.is_dir() else []
        older = [folder for folder in packaged + [_appdata() / 'Claude'] if folder.is_dir() and not any(folder.glob('.user-data-left*'))]
        return moved + older

    def path(self):
        folders = self._folders()
        for folder in folders:
            if (folder / self.FILE).is_file():
                return folder / self.FILE
        return (folders[0] if folders else _appdata() / 'Claude') / self.FILE

    def found(self):
        return bool(self._folders())

    def running(self, programs=None):
        """Claude Desktop keeps its settings in memory while it runs and saves them back whole (it did, over a fresh
        juno entry, after the rename), so a change made while it is open can vanish. Its program is claude.exe, the
        same name as Claude Code's, so it is told apart by where it is installed: the Store package or the older
        AnthropicClaude folder."""
        homes = ('\\windowsapps\\claude_', '\\anthropicclaude\\')
        return any(path.endswith('\\claude.exe') and any(home in path for home in homes)
                   for path in (running_programs() if programs is None else programs))


class Cursor(JsonTool):
    id, name = 'cursor', 'Cursor'

    def path(self):
        return Path.home() / '.cursor' / 'mcp.json'


class VSCode(JsonTool):
    """VS Code's user-level MCP servers: <user folder>\\mcp.json, under "servers"."""
    id, name, folder = 'vscode', 'VS Code (Copilot)', 'Code'
    key, extra = 'servers', {'type': 'stdio'}

    def path(self):
        return _appdata() / self.folder / 'User' / 'mcp.json'


class VSCodeInsiders(VSCode):
    id, name, folder = 'vscode_insiders', 'VS Code Insiders', 'Code - Insiders'


class Windsurf(JsonTool):
    id, name = 'windsurf', 'Windsurf'

    def path(self):
        return Path.home() / '.codeium' / 'windsurf' / 'mcp_config.json'


class GeminiCLI(JsonTool):
    id, name = 'gemini', 'Gemini CLI'

    def path(self):
        return Path.home() / '.gemini' / 'settings.json'

    def found(self):  # ~/.gemini alone is shared with other Google tools
        return self.path().is_file() or bool(shutil.which('gemini')) or (_appdata() / 'npm' / 'gemini.cmd').is_file()


TOOLS = [ClaudeCode(), Codex(), ClaudeDesktop(), Cursor(), VSCode(), VSCodeInsiders(), Windsurf(), GeminiCLI()]


def found_tools():
    return [tool for tool in TOOLS if tool.found()]


def tool(tool_id):
    return next(t for t in TOOLS if t.id == tool_id)


def open_tools(names, programs=None):
    """Of the tools named, those open right now that must be quit and reopened to keep Juno's change."""
    programs = running_programs() if programs is None else programs
    return [tool.name for tool in TOOLS if tool.name in names and tool.running(programs)]


def move_from_alfred(tools=None):
    """Juno was called Alfred until November 2026: every AI tool that still has Alfred's MCP server (entry "alfred") gets
    Juno's ("juno") in its place. Tools without an old entry are not touched. Returns the names of the tools moved over;
    a tool that fails keeps its old entry and is tried again at the next start."""
    import logging
    log = logging.getLogger('juno')
    moved = []
    for tool in TOOLS if tools is None else tools:
        try:
            if tool.entry(OLD_NAME) is None or not tool.found():
                continue
            tool.add()
            tool.disconnect(OLD_NAME)
            moved.append(tool.name)
        except (ConnectError, OSError) as error:
            log.warning('Could not move %s from Alfred to Juno: %s', tool.name, error)
    if moved:
        log.info('AI tools moved from Alfred to Juno: %s', ', '.join(moved))
    return moved
