"""PLACEHOLDER: watching the search folder for changes is not part of Alfred yet.

Same interface as the real watcher will have; it never reports changes.
Replace this file with the real module later.
"""
from PySide6.QtCore import QObject, Signal


class FolderWatcher(QObject):
    changed = Signal()

    def __init__(self, debounce_ms=2500, parent=None):
        super().__init__(parent)
        self.root = None

    @property
    def active(self):
        return False

    def set_root(self, root):
        self.root = None

    def stop(self):
        self.root = None
