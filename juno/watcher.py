"""Watch the tracked folders and ask for an incremental re-index when they change.

Uses Qt's QFileSystemWatcher (no extra dependency). Folders are watched
recursively by registering each subfolder; on Windows a directory notification
fires when files are added, removed, renamed or written. Events are debounced so
a large copy finishes before indexing starts.
"""
import os
import stat as file_stat
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from . import paths

MAX_DIRS = 6000


class FolderWatcher(QObject):
    changed = Signal()

    def __init__(self, debounce_ms=2000, parent=None):
        super().__init__(parent)
        self.roots = []
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_change)
        self.timer = QTimer(self, singleShot=True, interval=debounce_ms)
        self.timer.timeout.connect(self.changed.emit)

    @property
    def active(self):
        return bool(self.roots and self.watcher.directories())

    def set_roots(self, roots):
        """Watch these folders and their subfolders (replacing what was watched before)."""
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        self.roots = [Path(os.path.realpath(r)) for r in roots if Path(r).is_dir()]
        for root in self.roots:
            self._watch_tree(root)

    def stop(self):
        self.set_roots([])
        self.timer.stop()

    def _watch_tree(self, top):
        watched = set(self.watcher.directories())
        pending, found = [Path(top)], []
        while pending and len(watched) + len(found) < MAX_DIRS:
            base = pending.pop()
            found.append(str(base))
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not entry.is_dir(follow_symlinks=False) or file_stat.S_ISLNK(st.st_mode) or paths.skip_dir(entry.name) \
                        or getattr(st, 'st_file_attributes', 0) & (paths.REPARSE | paths.HIDDEN_SYSTEM):
                    continue
                pending.append(Path(entry.path))
        new = [d for d in found if d not in watched]
        if new:
            self.watcher.addPaths(new)

    def _on_change(self, directory):
        if not self.roots:
            return
        if Path(directory).is_dir():
            self._watch_tree(Path(directory))  # pick up new subfolders
        self.timer.start()  # restart: wait for the folder to settle
