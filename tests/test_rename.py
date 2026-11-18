"""Juno was called Alfred until November 2026: what an Alfred install leaves behind moves over to Juno at the first
start (data folder, saved API keys, AI-tool entries, shortcuts). Temp folders and fakes only: no real settings, key,
AI tool, shortcut or registry key is touched.

Run from the repository folder:  .venv\\Scripts\\python -m unittest tests.test_rename -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from juno import config, connect, keys, shortcut, startup  # noqa: E402

CLEAN_ENV = {name: '' for name in connect.PASSED_ENV}


class DataFolderTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='juno-rename-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        config._move_failed.clear()

    def test_alfreds_folder_moves_to_juno(self):
        (self.root / 'Alfred' / 'Chats').mkdir(parents=True)
        (self.root / 'Alfred' / 'settings.json').write_text('{"theme": "dark"}', encoding='utf-8')
        self.assertEqual(config.move_from_alfred(self.root), self.root / 'Juno')
        self.assertFalse((self.root / 'Alfred').exists())
        self.assertEqual((self.root / 'Juno' / 'settings.json').read_text(encoding='utf-8'), '{"theme": "dark"}')
        self.assertTrue((self.root / 'Juno' / 'Chats').is_dir())

    def test_an_existing_juno_folder_wins(self):
        (self.root / 'Alfred').mkdir()
        (self.root / 'Juno').mkdir()
        self.assertEqual(config.move_from_alfred(self.root), self.root / 'Juno')
        self.assertTrue((self.root / 'Alfred').is_dir())  # left alone

    def test_nothing_to_move(self):
        self.assertEqual(config.move_from_alfred(self.root), self.root / 'Juno')
        self.assertFalse((self.root / 'Juno').exists())  # data_home creates it, not the move

    def test_a_folder_still_in_use_is_used_where_it_is(self):
        (self.root / 'Alfred').mkdir()
        with mock.patch.object(config.os, 'rename', side_effect=PermissionError('in use')), \
                self.assertLogs('juno', 'WARNING') as logs:
            self.assertEqual(config.move_from_alfred(self.root), self.root / 'Alfred')
            self.assertEqual(config.move_from_alfred(self.root), self.root / 'Alfred')
        self.assertEqual(len(logs.records), 1)  # said once, not at every call
        self.assertEqual(config.move_from_alfred(self.root), self.root / 'Juno')  # free again: moved

    def test_data_home_moves_it(self):
        (self.root / 'Alfred').mkdir()
        with mock.patch.dict(os.environ, {'JUNO_HOME': '', 'LOCALAPPDATA': str(self.root)}):
            self.assertEqual(config.data_home(), self.root / 'Juno')
        self.assertFalse((self.root / 'Alfred').exists())

    def test_juno_home_is_never_moved_into(self):
        (self.root / 'Alfred').mkdir()
        with mock.patch.dict(os.environ, {'JUNO_HOME': str(self.root / 'elsewhere'), 'LOCALAPPDATA': str(self.root)}):
            self.assertEqual(config.data_home(), self.root / 'elsewhere')
        self.assertTrue((self.root / 'Alfred').is_dir())


class FakeKeyring:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.stored:
            raise KeyError(name)
        del self.stored[(service, name)]


class KeysTest(unittest.TestCase):
    def test_a_key_saved_by_alfred_moves_over_when_read(self):
        ring = FakeKeyring({('Alfred', 'openai'): 'sk-old'})
        with mock.patch.object(keys, '_backend', return_value=ring):
            self.assertEqual(keys.get('openai'), 'sk-old')
        self.assertEqual(ring.stored, {('Juno', 'openai'): 'sk-old'})

    def test_junos_own_key_wins(self):
        ring = FakeKeyring({('Alfred', 'openai'): 'sk-old', ('Juno', 'openai'): 'sk-new'})
        with mock.patch.object(keys, '_backend', return_value=ring):
            self.assertEqual(keys.get('openai'), 'sk-new')
        self.assertIn(('Alfred', 'openai'), ring.stored)

    def test_delete_removes_both(self):
        ring = FakeKeyring({('Alfred', 'openai'): 'a', ('Juno', 'openai'): 'b'})
        with mock.patch.object(keys, '_backend', return_value=ring):
            keys.delete('openai')
        self.assertEqual(ring.stored, {})


class FakeJsonTool(connect.JsonTool):
    id, name = 'fake', 'Fake'

    def __init__(self, path):
        self._path = Path(path)

    def path(self):
        return self._path


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='juno-tools-'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        env = mock.patch.dict(os.environ, CLEAN_ENV)
        env.start()
        self.addCleanup(env.stop)

    def test_alfreds_entry_is_replaced_by_junos(self):
        path = self.folder / 'mcp.json'
        old = {'command': r'C:\old\pythonw.exe', 'args': ['-m', 'alfred', '--mcp'], 'env': {}}
        path.write_text(json.dumps({'mcpServers': {'alfred': old, 'other': {'command': 'x'}}}), encoding='utf-8')
        tool = FakeJsonTool(path)
        self.assertEqual(connect.move_from_alfred([tool]), ['Fake'])
        servers = json.loads(path.read_text(encoding='utf-8'))['mcpServers']
        self.assertEqual(servers, {'juno': connect.server_spec(), 'other': {'command': 'x'}})

    def test_a_tool_without_alfred_is_not_touched(self):
        path = self.folder / 'mcp.json'
        path.write_text(json.dumps({'mcpServers': {'other': {'command': 'x'}}}), encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(connect.move_from_alfred([FakeJsonTool(path)]), [])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.folder / 'mcp.json.before-juno').exists())

    def test_a_failing_tool_keeps_its_entry_and_the_rest_carry_on(self):
        broken, fine = FakeJsonTool(self.folder / 'a.json'), FakeJsonTool(self.folder / 'b.json')
        for tool in (broken, fine):
            tool.path().write_text(json.dumps({'mcpServers': {'alfred': {'command': 'x'}}}), encoding='utf-8')
        with mock.patch.object(broken, 'add', side_effect=connect.ConnectError('read-only')), self.assertLogs('juno', 'WARNING'):
            self.assertEqual(connect.move_from_alfred([broken, fine]), ['Fake'])
        self.assertIn('alfred', json.loads(broken.path().read_text(encoding='utf-8'))['mcpServers'])
        self.assertNotIn('alfred', json.loads(fine.path().read_text(encoding='utf-8'))['mcpServers'])

    def test_claude_code_removes_alfred_everywhere(self):
        project = self.folder / 'project'
        project.mkdir()
        config_file = {'mcpServers': {'other': {}}, 'projects': {str(project): {'mcpServers': {'alfred': {}}}}}
        (self.folder / '.claude.json').write_text(json.dumps(config_file), encoding='utf-8')
        tool = connect.ClaudeCode()
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.folder)}), \
                mock.patch.object(connect.ClaudeCode, 'exe', return_value='claude'), \
                mock.patch.object(connect, '_run', return_value=(0, '')) as run:
            self.assertEqual(tool.entry(connect.OLD_NAME), {})  # only a per-folder entry: still to move
            self.assertEqual(connect.move_from_alfred([tool]), ['Claude Code'])
        calls = [(c.args[0], c.kwargs.get('cwd')) for c in run.call_args_list]
        self.assertIn((['claude', 'mcp', 'remove', 'alfred', '-s', 'user'], None), calls)
        self.assertIn((['claude', 'mcp', 'remove', 'alfred', '-s', 'local'], str(project)), calls)
        self.assertTrue(any(args[:4] == ['claude', 'mcp', 'add', 'juno'] for args, _ in calls))


@unittest.skipUnless(os.name == 'nt', 'Windows shortcuts')
class ShortcutsTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='juno-shortcuts-'))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.startup, self.menu = self.folder / 'Startup', self.folder / 'Programs'
        self.startup.mkdir()
        self.menu.mkdir()
        for patcher in (mock.patch('subprocess.Popen', side_effect=AssertionError('a program was started')),
                        mock.patch('winreg.DeleteKey')):  # never the real registry
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_alfreds_shortcuts_become_junos(self):
        for folder in (self.startup, self.menu):
            shortcut.write(folder / 'Alfred.lnk', sys.executable, '-m alfred --startup', startup.REPO, sys.executable)
        replaced = startup.move_from_alfred(self.startup, self.menu)
        self.assertEqual(sorted(p.parent.name for p in replaced), ['Programs', 'Startup'])
        self.assertFalse(any(self.folder.rglob('Alfred.lnk')))
        self.assertTrue(startup.enabled(self.startup))
        self.assertEqual(shortcut.read(self.menu / 'Juno.lnk')['arguments'], startup.command(at_sign_in=False)[1])

    def test_another_alfreds_shortcut_is_left_alone(self):
        shortcut.write(self.startup / 'Alfred.lnk', sys.executable, '', self.folder, sys.executable)
        self.assertEqual(startup.move_from_alfred(self.startup, self.menu), [])
        self.assertTrue((self.startup / 'Alfred.lnk').is_file())
        self.assertFalse((self.startup / 'Juno.lnk').exists())


if __name__ == '__main__':
    unittest.main()
