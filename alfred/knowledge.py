"""PLACEHOLDER: saving answers as Obsidian notes is not part of Alfred yet.

Same names as the real module will have, so ui.py runs unchanged. The vault always reports
"not set up"; anything that would read or write notes raises VaultError.
Replace this file with the real module later.
"""
NOT_READY = 'Obsidian notes are not set up in this version of Alfred yet.'


class VaultError(ValueError):
    pass


class NoteExists(VaultError):
    def __init__(self, rel):
        super().__init__(f'A note named {rel} already exists')
        self.rel = rel


def vault_status(settings):
    return {'state': 'unconfigured', 'message': NOT_READY}


def notes_folder(settings):
    return (settings.get('notes_folder') or 'Alfred').strip().replace('\\', '/').strip('/')


def require_vault(settings):
    raise VaultError(NOT_READY)


def list_notes(settings, limit=50, query=None, folder_only=False):
    raise VaultError(NOT_READY)


def suggest_links(settings, title, text):
    return []


def draft_note(question, answer, citations, links=(), title=None):
    title = title or (question.strip()[:80] or 'Untitled')
    return title, f'# {title}\n\n{answer}\n'


def note_path(settings, title):
    raise VaultError(NOT_READY)


def save_note(settings, title, content, overwrite=False):
    raise VaultError(NOT_READY)


def unique_title(settings, title):
    return title


def obsidian_uri(path):
    return ''


def vault_uri(settings):
    raise VaultError(NOT_READY)
