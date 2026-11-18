"""bundle_models.py DIST_FOLDER CACHE_FOLDER: put what search by meaning needs into a build, so it works without a download.

Into DIST_FOLDER (the folder holding Juno.exe) it puts
  runtime\\llama\\                         the pinned llama.cpp build (llama-server.exe, its DLLs and licence files)
  models\\<folder>\\<name>                  each file in models.FIRST_RUN (the EmbeddingGemma 2 text model)
  models\\<folder>\\LICENSE-Apache-2.0.txt  the model's licence, which must travel with it
Both are found there at run time (runtime.program_dir, models.find). Files are downloaded once into CACHE_FOLDER and
checked by their pinned SHA-256 (models.BUILD, models.FILES) every time, so a changed or partial file never ships.
The picture projector (about 1 GB) is not bundled: Juno still offers it as a download in Settings.
"""
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from juno import models  # noqa: E402

# The model licence is an asset of the same release as the model file.
LICENCE = ('LICENSE-Apache-2.0.txt', models.RELEASE + 'LICENSE-Apache-2.0.txt',
           'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(4 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def progress(label):
    state = {'shown': -1}

    def show(done, total):
        tenth = done * 10 // max(total, 1)
        if tenth != state['shown']:
            state['shown'] = tenth
            print(f'  {label}: {done / 1e6:,.0f} of {total / 1e6:,.0f} MB', flush=True)
    return show


def cached(label, urls, size, digest, target):
    """``target`` in the cache, downloaded if absent, and checked by SHA-256 either way."""
    target = Path(target)
    if not target.is_file():
        print(f'Downloading {label}', flush=True)
        models._fetch(label, urls, size, digest, target, progress(label), None)
    if target.stat().st_size != size or sha256(target) != digest:
        target.unlink()
        raise SystemExit(f'{label} in the cache did not match its checksum and was deleted; run the build again')
    return target


def licence_file(cache):
    name, url, digest = LICENCE
    target = Path(cache) / name
    if not target.is_file():
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Juno'}), timeout=60) as response:
            target.write_bytes(response.read())
    if sha256(target) != digest:
        target.unlink()
        raise SystemExit(f'{name} did not match its checksum and was deleted')
    return target


def main(dist, cache):
    dist, cache = Path(dist).resolve(), Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if not (dist / 'Juno.exe').is_file():
        raise SystemExit(f'{dist} has no Juno.exe')

    build = models.BUILD
    archive = cached(build['label'], build['urls'], build['size'], build['sha256'], cache / build['zip'])
    engine = dist / 'runtime' / 'llama'
    models.unpack_engine(archive, engine)
    print(f'Engine: {engine} ({sum(p.stat().st_size for p in engine.rglob("*") if p.is_file()) / 1e6:,.0f} MB)')

    licence = licence_file(cache)
    for key in models.FIRST_RUN:
        if key == models.ENGINE:
            continue
        file = models.FILES[key]
        source = cached(file.label, file.urls, file.size, file.sha256, cache / file.name)
        folder = dist / 'models' / file.folder
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, folder / file.name)
        shutil.copyfile(licence, folder / licence.name)
        if sha256(folder / file.name) != file.sha256:
            raise SystemExit(f'{file.name} changed while it was copied into the build')
        print(f'Model: {folder / file.name} ({file.size / 1e6:,.0f} MB)')


if __name__ == '__main__':
    main(*sys.argv[1:3])
