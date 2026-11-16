"""Adding Alfred's MCP server to AI tools (alfred/connect.py), without touching any real tool's settings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alfred import connect


class FakeJsonTool(connect.JsonTool):
    id, name = 'fake', 'Fake'

    def __init__(self, path, key='mcpServers', extra=None):
        self._path, self.key, self.extra = Path(path), key, extra or {}

    def path(self):
        return self._path


class ServerSpecTest(unittest.TestCase):
    def test_source_install_puts_its_folder_on_the_path(self):
        spec = connect.server_spec()
        repo = str(Path(connect.__file__).resolve().parent.parent)
        self.assertEqual(spec['args'], ['-m', 'alfred', '--mcp'])
        self.assertEqual(spec['env'], {'PYTHONPATH': repo})  # tools start it from their own folder
        self.assertTrue(Path(spec['command']).is_file())
        self.assertEqual(json.loads(connect.other_tool_json())['mcpServers']['alfred'], spec)


class JsonToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'mcp.json'

    def tearDown(self):
        self.dir.cleanup()

    def test_connect_keeps_other_servers_and_settings(self):
        self.path.write_text(json.dumps({'theme': 'dark', 'mcpServers': {'other': {'command': 'x'}}}), encoding='utf-8')
        tool = FakeJsonTool(self.path)
        self.assertEqual(tool.status()['state'], 'not_added')
        tool.connect()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(data['mcpServers']['other'], {'command': 'x'})
        self.assertEqual(data['mcpServers']['alfred'], connect.server_spec())
        self.assertEqual(tool.status()['state'], 'connected')
        self.assertTrue((Path(self.dir.name) / 'mcp.json.before-alfred').is_file())  # the original, once
        tool.disconnect()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertNotIn('alfred', data['mcpServers'])
        self.assertIn('other', data['mcpServers'])

    def test_creates_the_file_and_uses_the_tools_own_key(self):
        tool = FakeJsonTool(Path(self.dir.name) / 'new' / 'mcp.json', key='servers', extra={'type': 'stdio'})
        tool.connect()
        entry = json.loads(tool.path().read_text(encoding='utf-8'))['servers']['alfred']
        self.assertEqual(entry['type'], 'stdio')
        self.assertEqual(tool.status()['state'], 'connected')

    def test_an_old_entry_is_offered_as_an_update(self):
        self.path.write_text(json.dumps({'mcpServers': {'alfred': {'command': 'old.exe', 'args': ['--mcp']}}}), encoding='utf-8')
        self.assertEqual(FakeJsonTool(self.path).status()['state'], 'outdated')

    def test_json_with_comments_is_left_alone(self):
        text = '{\n  // my servers\n  "mcpServers": {}\n}\n'
        self.path.write_text(text, encoding='utf-8')
        with self.assertRaises(connect.ConnectError):
            FakeJsonTool(self.path).connect()
        self.assertEqual(self.path.read_text(encoding='utf-8'), text)


class CliToolTest(unittest.TestCase):
    def test_claude_code_is_added_for_every_folder_and_old_folder_entries_go(self):
        tool = connect.ClaudeCode()
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(connect.ClaudeCode, 'exe', return_value='claude.exe'), \
                mock.patch.object(connect.ClaudeCode, 'folder_entries', return_value=[folder]), \
                mock.patch.object(connect, '_run', side_effect=lambda args, cwd=None: calls.append((args, cwd)) or (0, '')):
            tool.connect()
        spec = connect.server_spec()
        self.assertEqual(calls[0][0], ['claude.exe', 'mcp', 'remove', 'alfred', '-s', 'user'])
        self.assertEqual(calls[1], (['claude.exe', 'mcp', 'remove', 'alfred', '-s', 'local'], folder))
        self.assertEqual(calls[2][0], ['claude.exe', 'mcp', 'add', 'alfred', '-s', 'user', '-e', f"PYTHONPATH={spec['env']['PYTHONPATH']}",
                                       '--', spec['command'], '-m', 'alfred', '--mcp'])

    def test_codex_add(self):
        calls = []
        with mock.patch.object(connect.Codex, 'exe', return_value='codex.exe'), \
                mock.patch.object(connect.Codex, 'entry', return_value=None), \
                mock.patch.object(connect, '_run', side_effect=lambda args, cwd=None: calls.append(args) or (0, '')):
            connect.Codex().connect()
        spec = connect.server_spec()
        self.assertEqual(calls, [['codex.exe', 'mcp', 'add', 'alfred', '--env', f"PYTHONPATH={spec['env']['PYTHONPATH']}",
                                  '--', spec['command'], '-m', 'alfred', '--mcp']])

    def test_a_failed_add_is_reported(self):
        with mock.patch.object(connect.Codex, 'exe', return_value='codex.exe'), \
                mock.patch.object(connect.Codex, 'entry', return_value=None), \
                mock.patch.object(connect, '_run', return_value=(1, 'boom')):
            with self.assertRaisesRegex(connect.ConnectError, 'boom'):
                connect.Codex().connect()


if __name__ == '__main__':
    unittest.main()
