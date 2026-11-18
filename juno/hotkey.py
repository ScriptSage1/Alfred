"""System-wide shortcut via RegisterHotKey; no administrator access required."""
import ctypes
import os
import re

HOTKEY_ID = 0xD160

def hotkey_parts(text):
    modifiers = {'ctrl': 2, 'alt': 1, 'shift': 4, 'win': 8}
    keys = {'space': 0x20, 'enter': 0x0D, 'tab': 0x09}
    parts = [part.strip().lower() for part in text.split('+')]
    if len(parts) < 2 or any(p not in modifiers for p in parts[:-1]):
        raise ValueError('Use a modifier and key, such as Ctrl+Shift+Space.')
    mask = 0x4000  # MOD_NOREPEAT
    for part in parts[:-1]:
        mask |= modifiers[part]
    key = parts[-1]
    if key in keys:
        code = keys[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        code = ord(key.upper())
    elif re.fullmatch(r'f([1-9]|1[0-9]|2[0-4])', key):
        code = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError('Supported keys: Space, Enter, Tab, letters, digits, F1–F24.')
    return mask, code

class GlobalHotkey:
    def __init__(self):
        self.current = ''
    def register(self, text):
        mask, key = hotkey_parts(text)
        if os.name != 'nt':
            raise OSError('Global shortcuts require Windows.')
        old = self.current
        self.unregister()
        if not ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mask, key):
            if old:
                previous = hotkey_parts(old)
                ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, *previous)
                self.current = old
            raise OSError('This shortcut is already in use. Choose another combination.')
        self.current = text
    def unregister(self):
        if os.name == 'nt' and self.current:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        self.current = ''
