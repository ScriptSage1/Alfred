"""Chat history storage tests.  Run:  .venv\\Scripts\\python -m unittest discover -s tests -v"""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import chats, config  # noqa: E402


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / 'Chats'
        self.store = chats.ChatStore(self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, question='Summarize the budget decisions', answer='Two budgets were approved.'):
        chat = self.store.new_chat(question, 'gemma3:4b')
        chat['messages'] += [{'role': 'user', 'content': question, 'time': time.time(), 'scope': None},
                             {'role': 'assistant', 'content': answer, 'sources': [{'marker': 'S1', 'path': 'C:/x/budget.xlsx',
                              'filename': 'budget.xlsx', 'location': 'Costs, rows 1–2', 'segment_id': 3}], 'error': None}]
        return chat

    def test_save_and_reload(self):
        chat = self.chat()
        path = self.store.save(chat)
        self.assertTrue(path.name.endswith('Summarize the budget decisions.json'))
        self.assertRegex(path.name, r'^\d{4}-\d\d-\d\d \d\d-\d\d ')
        loaded = self.store.load(chat['id'])
        self.assertEqual(loaded['messages'][1]['sources'][0]['filename'], 'budget.xlsx')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['title'], 'Summarize the budget decisions')
        self.assertNotIn('_path', json.loads(path.read_text(encoding='utf-8')))
        self.assertEqual([c['count'] for c in self.store.list()], [1])

    def test_folder_is_created_and_no_temp_files_left(self):
        self.assertFalse(self.folder.exists())
        self.store.save(self.chat())
        self.assertEqual([p.suffix for p in self.folder.iterdir()], ['.json'])

    def test_awkward_titles_become_safe_file_names(self):
        for question in ('What is in C:\\Users\\me\\*.pdf? <now>', 'CON', '   ', 'a' * 300):
            chat = self.chat(question)
            path = self.store.save(chat)
            self.assertTrue(path.exists(), question)
            self.assertNotRegex(path.stem, r'[<>:"/\\|?*]')
        self.assertEqual(chats.title_from('   '), 'New chat')
        self.assertTrue(chats.title_from('word ' * 40).endswith('…'))
        self.assertLessEqual(len(chats.title_from('word ' * 40)), chats.TITLE_CHARS + 1)

    def test_same_title_same_minute_does_not_overwrite(self):
        first, second = self.chat('Same question'), self.chat('Same question')
        second['created'] = first['created']
        self.store.save(first)
        self.store.save(second)
        self.assertEqual(len(self.store.list()), 2)

    def test_rename_moves_the_file_and_keeps_order(self):
        old, new = self.chat('Older chat'), self.chat('Newer chat')
        self.store.save(old)
        time.sleep(0.01)
        self.store.save(new)
        self.store.rename(old['id'], 'Renamed chat')
        self.assertEqual([c['title'] for c in self.store.list()], ['Newer chat', 'Renamed chat'])
        self.assertEqual(len(list(self.folder.glob('*.json'))), 2)
        self.assertEqual(self.store.load(old['id'])['title'], 'Renamed chat')

    def test_continuing_a_chat_updates_the_same_file(self):
        chat = self.chat()
        self.store.save(chat)
        chat['messages'].append({'role': 'user', 'content': 'And the costs?'})
        self.store.save(chat)
        self.assertEqual(len(list(self.folder.glob('*.json'))), 1)
        self.assertEqual(len(self.store.load(chat['id'])['messages']), 3)

    def test_delete_restore_and_delete_all(self):
        a, b = self.chat('First'), self.chat('Second')
        self.store.save(a)
        self.store.save(b)
        removed = self.store.delete(a['id'])
        self.assertEqual(removed['title'], 'First')
        self.assertIsNone(self.store.load(a['id']))
        removed.pop('_path')
        self.store.save(removed, touch=False)  # undo
        self.assertIsNotNone(self.store.load(a['id']))
        (self.folder / 'notes.json').write_text('{"not": "a chat"}', encoding='utf-8')
        self.assertEqual(self.store.delete_all(), 2)
        self.assertTrue((self.folder / 'notes.json').exists())  # only chats are deleted
        self.assertEqual(self.store.list(), [])

    def test_filter_searches_titles_and_messages(self):
        self.store.save(self.chat('Trip ideas', 'Goa in December'))
        self.store.save(self.chat('Budget', 'Electricity costs went up'))
        self.assertEqual([c['title'] for c in self.store.list('goa')], ['Trip ideas'])
        self.assertEqual([c['title'] for c in self.store.list('BUDGET')], ['Budget'])

    def test_broken_files_are_ignored(self):
        self.folder.mkdir()
        (self.folder / 'broken.json').write_text('{oops', encoding='utf-8')
        self.store.save(self.chat())
        self.assertEqual(len(self.store.list()), 1)

    def test_grouping_by_day(self):
        now = datetime(2026, 10, 23, 16, 0)
        stamp = lambda **delta: (now - timedelta(**delta)).timestamp()
        items = [{'id': '1', 'updated': stamp(hours=1)}, {'id': '2', 'updated': stamp(days=1)},
                 {'id': '3', 'updated': stamp(days=3)}, {'id': '4', 'updated': stamp(days=30)}]
        groups = chats.group(items, now)
        self.assertEqual([(name, [c['id'] for c in chats_]) for name, chats_ in groups],
                         [('Today', ['1']), ('Yesterday', ['2']), ('Previous 7 days', ['3']), ('Older', ['4'])])

    def test_settings_default_folder_and_switch(self):
        settings = config.load(Path(self.tmp.name) / 'home')
        self.assertTrue(settings['save_chat_history'])
        self.assertEqual(settings.chats_folder, Path(self.tmp.name) / 'home' / 'Chats')
        settings['chats_folder'] = str(self.folder)
        self.assertEqual(settings.chats_folder, self.folder)


if __name__ == '__main__':
    unittest.main()
