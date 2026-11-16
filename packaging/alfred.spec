# PyInstaller spec: a one-folder build, dist\Alfred\Alfred.exe (windowed) plus everything it needs in _internal.
# Build with packaging\build.cmd (it passes --distpath and --workpath outside the repository).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
                                                 VarStruct, VSVersionInfo)

REPO = Path(SPECPATH).resolve().parent
ICON = REPO / 'alfred' / 'assets' / 'alfred.ico'
VERSION = next(line.split("'")[1] for line in (REPO / 'alfred' / '__init__.py').read_text(encoding='utf-8').splitlines()
               if line.startswith('__version__'))
NUMBERS = tuple((list(map(int, VERSION.split('.'))) + [0, 0, 0, 0])[:4])

# The exe's Details tab (product, description, version). An exe without them looks more like malware to antivirus
# heuristics, which is part of why Microsoft Defender once flagged Alfred.exe.
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[StringFileInfo([StringTable('040904B0', [
              StringStruct('CompanyName', 'Alfred'),
              StringStruct('FileDescription', 'Alfred - search and answers from your own files'),
              StringStruct('FileVersion', VERSION),
              StringStruct('InternalName', 'Alfred'),
              StringStruct('LegalCopyright', 'Alfred contributors'),
              StringStruct('OriginalFilename', 'Alfred.exe'),
              StringStruct('ProductName', 'Alfred'),
              StringStruct('ProductVersion', VERSION)])]),
          VarFileInfo([VarStruct('Translation', [0x0409, 1200])])])

# Inference engines RapidOCR can use but Alfred does not (onnxruntime only), and libraries nothing here needs.
UNUSED_OCR = ('pytorch', 'paddle', 'openvino', 'mnn', 'tensorrt')
EXCLUDES = ['torch', 'paddle', 'openvino', 'MNN', 'tensorrt', 'tkinter', 'matplotlib', 'IPython', 'pytest',
            'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
            'PySide6.Qt3DCore', 'PySide6.QtMultimedia', 'PySide6.QtPdf', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
            'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtXml',
            'PySide6.QtConcurrent', 'PySide6.QtDBus', 'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtPrintSupport',
            'PySide6.QtUiTools']

datas = [(str(REPO / 'alfred' / 'assets'), 'alfred/assets'), (str(REPO / 'alfred' / 'data'), 'alfred/data')]
datas += collect_data_files('rapidocr', excludes=[f'**/{name}/**' for name in UNUSED_OCR])  # models and .yaml configs
datas += collect_data_files('imageio_ffmpeg')  # the ffmpeg binary
datas += copy_metadata('keyring')  # keyring finds its backends through entry points
binaries = collect_dynamic_libs('onnxruntime')  # onnxruntime-directml: DirectML.dll and the provider DLLs

hiddenimports = collect_submodules('alfred')
hiddenimports += collect_submodules('rapidocr', filter=lambda name: not any(f'.{u}' in name for u in UNUSED_OCR) and not name.endswith('.cli'))
hiddenimports += ['keyring.backends.Windows', 'win32ctypes.core']

a = Analysis(
    [str(Path(SPECPATH) / 'alfred_entry.py')],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    noarchive=False,
)

# Qt's translations of its own dialogs: Alfred is in English.
a.datas = [entry for entry in a.datas if '/translations/' not in entry[0].replace('\\', '/')]
# OpenCV's video reader (30 MB; RapidOCR only uses OpenCV on images, videos go through ffmpeg) and Qt's software
# OpenGL (20 MB; Alfred draws with widgets, and imports no Qt OpenGL or Quick module).
DROP = ('opencv_videoio_ffmpeg', 'opengl32sw.dll')
a.binaries = [entry for entry in a.binaries if not any(part in entry[0].replace('\\', '/').split('/')[-1] for part in DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Alfred',
    icon=str(ICON) if ICON.is_file() else None,
    version=VERSION_INFO,
    console=False,  # no console window; --mcp still talks over the pipes an AI tool gives it
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name='Alfred', upx=False)
