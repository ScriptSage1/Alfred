"""Windows shortcuts (.lnk files) through the shell's own IShellLinkW and IPersistFile interfaces, called with ctypes.

Juno used to write its Start menu and sign-in shortcuts by starting a hidden PowerShell. A program with no window
that starts a hidden PowerShell to write into the Startup folder is what malware does to start with Windows, and
Microsoft Defender flagged Juno.exe as a trojan for it. This does the same work inside Juno's own process and starts
no other program."""
import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes

CLSID_SHELL_LINK = '{00021401-0000-0000-C000-000000000046}'
IID_SHELL_LINK_W = '{000214F9-0000-0000-C000-000000000046}'
IID_PERSIST_FILE = '{0000010B-0000-0000-C000-000000000046}'
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
RPC_E_CHANGED_MODE = 0x80010106  # COM is already set up on this thread in the other mode: usable, not ours to undo
STGM_READ = 0
SW_SHOWNORMAL = 1
SW_SHOWMINNOACTIVE = 7
BUFFER = 32768  # characters; the longest path Windows allows

# Slots in the interfaces' method tables (IUnknown takes 0-2 in both).
QUERY_INTERFACE, RELEASE = 0, 2
GET_PATH, GET_DESCRIPTION, SET_DESCRIPTION, GET_WORKING_DIRECTORY, SET_WORKING_DIRECTORY = 3, 6, 7, 8, 9
GET_ARGUMENTS, SET_ARGUMENTS, GET_SHOW_CMD, SET_SHOW_CMD, GET_ICON_LOCATION, SET_ICON_LOCATION, SET_PATH = 10, 11, 14, 15, 16, 17, 20
LOAD, SAVE = 5, 6


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


def _ole32():
    if os.name != 'nt':
        raise OSError('Shortcuts are only made on Windows')
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    return ole32


def _check(result, what):
    if result < 0:
        raise OSError(f'{what} failed (HRESULT 0x{result & 0xFFFFFFFF:08X})')
    return result


def _guid(ole32, text):
    guid = _GUID()
    _check(ole32.CLSIDFromString(text, ctypes.byref(guid)), 'CLSIDFromString')
    return guid


def _method(obj, slot, *argtypes):
    """The interface method in ``slot`` of ``obj``'s method table, as a callable taking the arguments after ``this``."""
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[slot])
    return lambda *args: function(obj, *args)


@contextmanager
def _shell_link():
    """(IShellLinkW, IPersistFile) of a new shell link object; COM is set up for the call and released after."""
    ole32 = _ole32()
    started = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if started < 0 and (started & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
        _check(started, 'CoInitializeEx')
    link, file = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _check(ole32.CoCreateInstance(ctypes.byref(_guid(ole32, CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
                                      ctypes.byref(_guid(ole32, IID_SHELL_LINK_W)), ctypes.byref(link)), 'CoCreateInstance')
        _check(_method(link, QUERY_INTERFACE, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(_guid(ole32, IID_PERSIST_FILE)), ctypes.byref(file)), 'QueryInterface(IPersistFile)')
        yield link, file
    finally:
        for obj in (file, link):
            if obj.value:
                _method(obj, RELEASE)()
        if started >= 0:
            ole32.CoUninitialize()


def write(path, target, arguments='', workdir='', icon='', icon_index=0, show=SW_SHOWNORMAL, description=''):
    """Create or replace the shortcut ``path`` that starts ``target``."""
    text = wintypes.LPCWSTR
    with _shell_link() as (link, file):
        _check(_method(link, SET_PATH, text)(str(target)), 'SetPath')
        _check(_method(link, SET_ARGUMENTS, text)(str(arguments)), 'SetArguments')
        _check(_method(link, SET_WORKING_DIRECTORY, text)(str(workdir)), 'SetWorkingDirectory')
        _check(_method(link, SET_ICON_LOCATION, text, ctypes.c_int)(str(icon), int(icon_index)), 'SetIconLocation')
        _check(_method(link, SET_SHOW_CMD, ctypes.c_int)(int(show)), 'SetShowCmd')
        _check(_method(link, SET_DESCRIPTION, text)(str(description)), 'SetDescription')
        _check(_method(file, SAVE, text, wintypes.BOOL)(str(path), True), 'IPersistFile.Save')


def read(path):
    """What the shortcut ``path`` starts: a dict of target, arguments, workdir, icon, icon_index, show, description."""
    with _shell_link() as (link, file):
        _check(_method(file, LOAD, wintypes.LPCWSTR, wintypes.DWORD)(str(path), STGM_READ), 'IPersistFile.Load')
        buffer = ctypes.create_unicode_buffer(BUFFER)

        def text(slot, what):
            buffer.value = ''
            _check(_method(link, slot, wintypes.LPWSTR, ctypes.c_int)(buffer, BUFFER), what)
            return buffer.value

        found = {}
        buffer.value = ''
        _check(_method(link, GET_PATH, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(buffer, BUFFER, None, 0), 'GetPath')
        found['target'] = buffer.value
        found['arguments'] = text(GET_ARGUMENTS, 'GetArguments')
        found['workdir'] = text(GET_WORKING_DIRECTORY, 'GetWorkingDirectory')
        found['description'] = text(GET_DESCRIPTION, 'GetDescription')
        index = ctypes.c_int()
        buffer.value = ''
        _check(_method(link, GET_ICON_LOCATION, wintypes.LPWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_int))(
            buffer, BUFFER, ctypes.byref(index)), 'GetIconLocation')
        found['icon'], found['icon_index'] = buffer.value, index.value
        show = ctypes.c_int()
        _check(_method(link, GET_SHOW_CMD, ctypes.POINTER(ctypes.c_int))(ctypes.byref(show)), 'GetShowCmd')
        found['show'] = show.value
        return found
