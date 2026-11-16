"""make_zip.py DIST_FOLDER ZIP_PATH: zip a folder (compressed), its own name as the top folder in the archive.
Model files (.gguf) are stored as they are: quantised weights hardly compress, and deflating them only slows the build."""
import os
import sys
import zipfile
from pathlib import Path

STORED = ('.gguf',)


def main(folder, target):
    folder, target = Path(folder).resolve(), Path(target).resolve()
    temp = target.with_suffix('.zip.part')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                method = zipfile.ZIP_STORED if path.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                archive.write(path, Path(folder.name) / path.relative_to(folder), compress_type=method)
    os.replace(temp, target)
    print(f'{target} ({target.stat().st_size / 2 ** 20:.0f} MB)')


if __name__ == '__main__':
    main(*sys.argv[1:3])
