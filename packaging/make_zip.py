"""make_zip.py DIST_FOLDER ZIP_PATH: zip a folder (compressed), its own name as the top folder in the archive."""
import os
import sys
import zipfile
from pathlib import Path


def main(folder, target):
    folder, target = Path(folder).resolve(), Path(target).resolve()
    temp = target.with_suffix('.zip.part')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))
    os.replace(temp, target)
    print(f'{target} ({target.stat().st_size / 2 ** 20:.0f} MB)')


if __name__ == '__main__':
    main(*sys.argv[1:3])
