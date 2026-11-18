"""Adding Juno's MCP server to AI tools (juno/connect.py), without touching any real tool's settings."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from juno import connect

REPO = str(Path(connect.__file__).resolve().parent.parent)
CLEAN_ENV = {name: '' for name in connect.PASSED_ENV}


class FakeJsonTool(connect.JsonTool):
    id, name = 'fake', 'Fake'

    def __init__(self, path, key='mcpServers', extra=None):
        self._path, self.key, self.extra = Path(path), key, extra or {}

    def path(self):
        return self._path


class ServerSpecTest(unittest.TestCase):
    def test_source_install_puts_its_folder_on_the_path(self):
        with mock.patch.dict(os.environ, CLEAN_ENV):
            spec = connect.server_spec()
        self.assertEqual(spec['args'], ['-m', 'juno', '--mcp'])
        self.assertEqual(spec['env'], {'PYTHONPATH': REPO})  # tools start it from their own folder
        self.assertTrue(Path(spec['command']).is_file())
        self.assertEqual(Path(spec['command']).name.lower(), 'pythonw.exe')  # no console window
        with mock.patch.dict(os.environ, CLEAN_ENV):
            self.assertEqual(json.loads(connect.other_tool_json())['mcpServers']['juno'], connect.server_spec())

    def test_this_junos_data_and_model_folders_are_passed_on(self):
        with mock.patch.dict(os.environ, dict(CLEAN_ENV, JUNO_HOME=r'D:\JunoData', JUNO_MODELS_DIR=r'E:\models')):
            env = connect.server_spec()['env']
        self.assertEqual(env, {'JUNO_HOME': r'D:\JunoData', 'JUNO_MODELS_DIR': r'E:\models', 'PYTHONPATH': REPO})

    def test_packaged_build_runs_itself(self):
        with mock.patch.object(connect.sys, 'frozen', True, create=True), \
                mock.patch.object(connect.sys, 'executable', r'C:\Program Files\Juno\Juno.exe'), \
                mock.patch.dict(os.environ, CLEAN_ENV):
            self.assertEqual(connect.server_spec(), {'command': r'C:\Program Files\Juno\Juno.exe', 'args': ['--mcp'], 'env': {}})


class CheckServerTest(unittest.TestCase):
    """Starts the real server, from an unrelated folder, as an AI tool would."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, dict(CLEAN_ENV, JUNO_HOME=self.home.name))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.home.cleanup()

    def test_the_entry_juno_writes_starts(self):
        connect.check_server(connect.server_spec())

    def test_without_the_install_folder_it_fails_and_says_why(self):
        spec = connect.server_spec()
        spec['env'] = {k: v for k, v in spec['env'].items() if k != 'PYTHONPATH'}  # the old copied command
        with self.assertRaisesRegex(connect.ConnectError, 'No module named juno'):
            connect.check_server(spec)

    def test_a_server_that_never_answers_times_out_and_is_stopped(self):
        import subprocess
        import sys
        marker = 'juno-check-hang-' + str(os.getpid())
        entry = {'command': sys.executable, 'args': ['-c', f'import time  # {marker}\ntime.sleep(120)'], 'env': {}}
        with self.assertRaisesRegex(connect.ConnectError, 'did not answer'):
            connect.check_server(entry, timeout=3)
        left = subprocess.run(['powershell', '-NoProfile', '-Command',
                               f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' -and "
                               "$_.Name -notlike 'powershell*' }).Count"],  # not this query itself
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, '0')  # the launcher and the interpreter it started

    def test_a_program_that_is_not_junos_server_is_rejected(self):
        import sys
        with self.assertRaisesRegex(connect.ConnectError, 'did not start'):
            connect.check_server({'command': sys.executable, 'args': ['-c', 'print("hello")'], 'env': {}})

    def test_a_missing_program_is_reported(self):
        with self.assertRaisesRegex(connect.ConnectError, 'missing'):
            connect.check_server({'command': r'C:\nowhere\python.exe', 'args': [], 'env': {}})


class JsonToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'mcp.json'
        self.check = mock.patch.object(connect, 'check_server')
        self.checked = self.check.start()

    def tearDown(self):
        self.check.stop()
        self.dir.cleanup()

    def test_connect_keeps_other_servers_and_settings(self):
        self.path.write_text(json.dumps({'theme': 'dark', 'mcpServers': {'other': {'command': 'x'}}}), encoding='utf-8')
        tool = FakeJsonTool(self.path)
        self.assertEqual(tool.status()['state'], 'not_added')
        tool.connect()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(data['mcpServers']['other'], {'command': 'x'})
        self.assertEqual(data['mcpServers']['juno'], connect.server_spec())
        self.checked.assert_called_once_with(data['mcpServers']['juno'])  # what the tool will run is what gets checked
        self.assertEqual(tool.status()['state'], 'connected')
        self.assertTrue((Path(self.dir.name) / 'mcp.json.before-juno').is_file())  # the original, once
        tool.disconnect()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertNotIn('juno', data['mcpServers'])
        self.assertIn('other', data['mcpServers'])

    def test_creates_the_file_and_uses_the_tools_own_key(self):
        tool = FakeJsonTool(Path(self.dir.name) / 'new' / 'mcp.json', key='servers', extra={'type': 'stdio'})
        tool.connect()
        entry = json.loads(tool.path().read_text(encoding='utf-8'))['servers']['juno']
        self.assertEqual(entry['type'], 'stdio')
        self.assertEqual(tool.status()['state'], 'connected')

    def test_a_file_with_a_bom_is_read(self):
        self.path.write_bytes(b'\xef\xbb\xbf' + json.dumps({'mcpServers': {}}).encode('utf-8'))
        tool = FakeJsonTool(self.path)
        tool.connect()
        self.assertEqual(tool.status()['state'], 'connected')

    def test_an_old_entry_is_offered_as_an_update(self):
        self.path.write_text(json.dumps({'mcpServers': {'juno': {'command': 'old.exe', 'args': ['--mcp']}}}), encoding='utf-8')
        self.assertEqual(FakeJsonTool(self.path).status()['state'], 'outdated')

    def test_json_with_comments_is_left_alone(self):
        text = '{\n  // my servers\n  "mcpServers": {}\n}\n'
        self.path.write_text(text, encoding='utf-8')
        with self.assertRaises(connect.ConnectError):
            FakeJsonTool(self.path).connect()
        self.assertEqual(self.path.read_text(encoding='utf-8'), text)
        self.checked.assert_not_called()

    def test_a_failed_check_is_reported(self):
        self.checked.side_effect = connect.ConnectError('The server did not start: boom')
        with self.assertRaisesRegex(connect.ConnectError, 'boom'):
            FakeJsonTool(self.path).connect()


class ToolLocationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.local, self.roaming = self.root / 'Local', self.root / 'Roaming'
        self.env = mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.local), 'APPDATA': str(self.roaming)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_claude_desktop_uses_the_folder_the_app_moved_to(self):
        moved = self.local / 'Claude-Data' / 'claude_pkg'
        moved.mkdir(parents=True)
        (moved / 'claude_desktop_config.json').write_text('{"preferences": {"x": 1}}', encoding='utf-8')
        (self.roaming / 'Claude').mkdir(parents=True)
        (self.roaming / 'Claude' / '.user-data-left.claude_pkg').write_text('', encoding='utf-8')
        tool = connect.ClaudeDesktop()
        self.assertTrue(tool.found())
        self.assertEqual(tool.path(), moved / 'claude_desktop_config.json')
        with mock.patch.object(connect, 'check_server'):
            tool.connect()
        data = json.loads((moved / 'claude_desktop_config.json').read_text(encoding='utf-8'))
        self.assertEqual(data['preferences'], {'x': 1})
        self.assertIn('juno', data['mcpServers'])
        self.assertFalse((self.roaming / 'Claude' / 'claude_desktop_config.json').exists())

    def test_claude_desktop_fresh_data_folder_without_a_config_yet(self):
        (self.local / 'Claude-Data' / 'claude_pkg').mkdir(parents=True)
        self.assertEqual(connect.ClaudeDesktop().path(), self.local / 'Claude-Data' / 'claude_pkg' / 'claude_desktop_config.json')

    def test_claude_desktop_older_builds_use_appdata(self):
        (self.roaming / 'Claude').mkdir(parents=True)
        self.assertEqual(connect.ClaudeDesktop().path(), self.roaming / 'Claude' / 'claude_desktop_config.json')

    def test_claude_desktop_not_installed(self):
        self.assertFalse(connect.ClaudeDesktop().found())

    def test_vscode_and_insiders(self):
        (self.roaming / 'Code - Insiders' / 'User').mkdir(parents=True)
        self.assertFalse(connect.VSCode().found())
        self.assertTrue(connect.VSCodeInsiders().found())
        self.assertEqual(connect.VSCodeInsiders().path(), self.roaming / 'Code - Insiders' / 'User' / 'mcp.json')

    def test_claude_code_and_codex_follow_their_config_folder_settings(self):
        claude_dir, codex_dir = self.root / 'cc', self.root / 'cx'
        claude_dir.mkdir()
        codex_dir.mkdir()
        (claude_dir / '.claude.json').write_text(json.dumps({'mcpServers': {'juno': {'command': 'a'}}}), encoding='utf-8')
        (codex_dir / 'config.toml').write_text('[mcp_servers.juno]\ncommand = "b"\nargs = []\n', encoding='utf-8')
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(claude_dir), 'CODEX_HOME': str(codex_dir)}):
            self.assertEqual(connect.ClaudeCode().entry(), {'command': 'a'})
            self.assertEqual(connect.Codex().entry()['command'], 'b')


class CliToolTest(unittest.TestCase):
    def test_claude_code_is_added_for_every_folder_and_old_folder_entries_go(self):
        tool = connect.ClaudeCode()
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(connect.ClaudeCode, 'exe', return_value='claude.exe'), \
                mock.patch.object(connect.ClaudeCode, 'folder_entries', return_value=[folder]), \
                mock.patch.object(connect, '_run', side_effect=lambda args, cwd=None: calls.append((args, cwd)) or (0, '')):
            tool.add()
        spec = connect.server_spec()
        env = [part for key, value in spec['env'].items() for part in ('-e', f'{key}={value}')]
        self.assertEqual(calls[0][0], ['claude.exe', 'mcp', 'remove', 'juno', '-s', 'user'])
        self.assertEqual(calls[1], (['claude.exe', 'mcp', 'remove', 'juno', '-s', 'local'], folder))
        self.assertEqual(calls[2][0], ['claude.exe', 'mcp', 'add', 'juno', '-s', 'user'] + env + ['--', spec['command'], '-m', 'juno', '--mcp'])

    def test_codex_add(self):
        calls = []
        with mock.patch.object(connect.Codex, 'exe', return_value='codex.exe'), \
                mock.patch.object(connect.Codex, 'entry', return_value=None), \
                mock.patch.object(connect, '_run', side_effect=lambda args, cwd=None: calls.append(args) or (0, '')):
            connect.Codex().add()
        spec = connect.server_spec()
        env = [part for key, value in spec['env'].items() for part in ('--env', f'{key}={value}')]
        self.assertEqual(calls, [['codex.exe', 'mcp', 'add', 'juno'] + env + ['--', spec['command'], '-m', 'juno', '--mcp']])

    def test_a_failed_add_is_reported(self):
        with mock.patch.object(connect.Codex, 'exe', return_value='codex.exe'), \
                mock.patch.object(connect.Codex, 'entry', return_value=None), \
                mock.patch.object(connect, '_run', return_value=(1, 'boom')):
            with self.assertRaisesRegex(connect.ConnectError, 'boom'):
                connect.Codex().connect()

    def test_connect_fails_when_the_tool_did_not_keep_the_entry(self):
        with mock.patch.object(connect.Codex, 'add'), mock.patch.object(connect.Codex, 'entry', return_value=None):
            with self.assertRaisesRegex(connect.ConnectError, 'did not keep'):
                connect.Codex().connect()


if __name__ == '__main__':
    unittest.main()
