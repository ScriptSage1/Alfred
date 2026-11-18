"""The script PyInstaller freezes into Juno.exe (juno\\__main__.py uses relative imports, so it cannot be the entry
itself). freeze_support comes first: a PDF-table worker process started from Juno.exe runs the worker and exits
here, before anything of Juno is imported."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from juno.__main__ import main
    sys.exit(main())
