"""Chat history: one readable JSON file per chat in a dedicated folder.

A chat is a dict::

    {'id': '3f2a9c1e5b7d', 'title': 'Summarize decisions', 'created': 1761236520.0, 'updated': ...,
     'model': 'gemma3:4b', 'messages': [
         {'role': 'user', 'content': '…', 'time': …, 'scope': None},
         {'role': 'assistant', 'content': '…', 'time': …, 'model': 'gemma3:4b',
          'sources': [{'marker': 'S1', 'path': '…', 'filename': '…', 'location': 'page 2', 'segment_id': 12}],
          'error': None}]}

Files are named "2026-10-23 16-52 Summarize decisions.json" and written atomically
(temp file, then replace), so a crash never leaves half a chat behind.
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

TITLE_CHARS = 60
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
_lock = threading.Lock()


def title_from(question):
    """A short title from the first question: one line, trimmed at a word boundary."""
    text = ' '.join((question or '').split())
    if len(text) <= TITLE_CHARS:
        return text or 'New chat'
    cut = text[:TITLE_CHARS].rsplit(' ', 1)[0]
    return (cut or text[:TITLE_CHARS]).rstrip(',.;:') + '…'


def safe_name(title):
    """Title as a Windows-safe file name part."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', title or '')
    name = ' '.join(name.split()).strip(' .')[:TITLE_CHARS] or 'Chat'
    return f'{name} chat' if name.upper() in RESERVED else name


def group(chats, now=None):
    """[('Today', [...]), ('Yesterday', [...]), ('Previous 7 days', [...]), ('Older', [...])], empty groups left out."""
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    bounds = [('Today', today), ('Yesterday', today - timedelta(days=1)), ('Previous 7 days', today - timedelta(days=7))]
    groups = {name: [] for name, _ in bounds}
    groups['Older'] = []
    for chat in chats:
        when = datetime.fromtimestamp(chat.get('updated') or chat.get('created') or 0)
        name = next((name for name, start in bounds if when >= start), 'Older')
        groups[name].append(chat)
    return [(name, items) for name, items in groups.items() if items]


class ChatStore:
    def __init__(self, folder):
        self.folder = Path(folder)

    @staticmethod
    def new_chat(question, model=''):
        now = time.time()
        return {'id': uuid.uuid4().hex[:12], 'title': title_from(question), 'created': now, 'updated': now, 'model': model,
                'messages': []}

    def _files(self):
        return sorted(self.folder.glob('*.json')) if self.folder.is_dir() else []

    def _read(self, file):
        try:
            chat = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(chat, dict) or not chat.get('id') or not isinstance(chat.get('messages'), list):
            return None
        chat['_path'] = str(file)
        return chat

    def _path_of(self, chat_id):
        for file in self._files():
            chat = self._read(file)
            if chat and chat['id'] == chat_id:
                return file
        return None

    def list(self, query=''):
        """Chats newest first, as summaries {'id', 'title', 'created', 'updated', 'model', 'count'}."""
        query = query.casefold().strip()
        chats = []
        for file in self._files():
            chat = self._read(file)
            if chat is None:
                continue
            if query and query not in chat.get('title', '').casefold() and \
                    not any(query in (m.get('content') or '').casefold() for m in chat['messages']):
                continue
            chats.append({'id': chat['id'], 'title': chat.get('title') or 'Chat', 'created': chat.get('created', 0),
                          'updated': chat.get('updated', 0), 'model': chat.get('model', ''),
                          'count': sum(1 for m in chat['messages'] if m.get('role') == 'user')})
        return sorted(chats, key=lambda c: -(c['updated'] or 0))

    def load(self, chat_id):
        file = self._path_of(chat_id)
        return self._read(file) if file else None

    def save(self, chat, touch=True):
        """Write the chat (creating the folder if needed); returns its file path. ``touch`` marks it as just used."""
        if touch or not chat.get('updated'):
            chat['updated'] = time.time()
        stamp = datetime.fromtimestamp(chat.get('created') or chat['updated']).strftime('%Y-%m-%d %H-%M')
        with _lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            old = Path(chat['_path']) if chat.get('_path') else self._path_of(chat['id'])
            target = self.folder / f'{stamp} {safe_name(chat.get("title"))}.json'
            if target.exists() and (old is None or os.path.normcase(str(target)) != os.path.normcase(str(old))):
                target = self.folder / f'{stamp} {safe_name(chat.get("title"))} {chat["id"][:4]}.json'
            data = {k: v for k, v in chat.items() if not k.startswith('_')}
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(target)
            if old is not None and old.exists() and os.path.normcase(str(old)) != os.path.normcase(str(target)):
                old.unlink()  # the title changed, so the file name did too
        chat['_path'] = str(target)
        return target

    def rename(self, chat_id, title):
        chat = self.load(chat_id)
        if chat is None:
            return None
        chat['title'] = ' '.join((title or '').split())[:120] or chat['title']
        self.save(chat, touch=False)
        return chat

    def delete(self, chat_id):
        file = self._path_of(chat_id)
        if file is None:
            return None
        chat = self._read(file)
        file.unlink()
        return chat

    def delete_all(self):
        count = 0
        for file in self._files():
            if self._read(file) is not None:
                file.unlink()
                count += 1
        return count
