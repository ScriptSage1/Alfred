"""PLACEHOLDER: file indexing and search are not part of Alfred yet.

Same names and return shapes as the real module will have, so ui.py runs unchanged.
The index is always empty: searches return no results and indexing finds nothing.
Replace this file with the real indexer later.
"""
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
VIDEO_SUFFIXES = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.wmv', '.mpeg', '.mpg'}


def video_frame_png(path, seconds, width=660):
    raise RuntimeError('Video previews are not set up in this version of Alfred yet.')


class Index:
    def __init__(self, settings):
        self.settings = settings

    @property
    def root(self):
        return self.settings.root

    def status(self):
        root = self.root
        return {'root': str(root), 'root_exists': root.is_dir(), 'initialized': False, 'files': 0, 'segments': 0,
                'vectors': 0, 'errors': 0, 'unsupported': 0, 'last_indexed': None, 'embed_model': None}

    def problems(self, limit=50):
        return []

    def pending_changes(self):
        return {'new': 0, 'changed': 0, 'removed': 0}

    def root_file_count(self):
        return 0

    def update(self, progress=lambda state: None, embedder=None, force=False, vision=None):
        progress({'phase': 'done', 'done': 0, 'total': 0})
        return {'found': 0, 'indexed': 0, 'unchanged': 0, 'removed': 0, 'errors': 0, 'embedded': 0, 'warnings': []}

    def rebuild(self, progress=lambda state: None, embedder=None, vision=None):
        return self.update(progress, embedder, force=True, vision=vision)

    def search(self, query, limit=10, mode='hybrid', embedder=None, per_file=2, path=None):
        return []

    def read_excerpt(self, rel, segment_id=None, page=None, line=None, max_chars=4000, single=False):
        raise RuntimeError('File previews are not set up in this version of Alfred yet.')
