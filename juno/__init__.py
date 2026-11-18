"""Juno: a personal desktop knowledge assistant opened with a global shortcut."""
import sys
from pathlib import Path

__version__ = '0.1.0'
APP_NAME = 'Juno'
# The project folder for a clone; the folder holding Juno.exe for a frozen (PyInstaller) build.
REPO = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
