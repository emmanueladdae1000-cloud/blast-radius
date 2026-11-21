"""Add exact pinned root configuration sources needed for semantic comparisons."""
import gzip
import io
from pathlib import Path
import shutil
import tarfile

project = Path('/audit')
base = Path('/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa')
archive = project / 'src.tar.gz'
backup = project / 'reports' / 'src-before-root-config.tar.gz'
backup.parent.mkdir(parents=True, exist_ok=True)
if not backup.exists():
    shutil.copyfile(archive, backup)
temporary = project / 'reports' / 'src-with-root-config.tar.gz'
added = []
with tarfile.open(archive, 'r:gz') as old, temporary.open('wb') as output:
    with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as new:
            names = set()
            for entry in old.getmembers():
                names.add(entry.name)
                if entry.isfile():
                    raw = old.extractfile(entry).read()
                    if (base / entry.name).read_bytes() != raw:
                        raise RuntimeError('Source is not pinned: ' + entry.name)
                    new.addfile(entry, io.BytesIO(raw))
                else:
                    new.addfile(entry)
            for name in ('pyproject.toml', 'setup.py', 'setup.cfg', 'tox.ini',
                         'pytest.ini', 'Makefile', 'conftest.py', 'LICENSE'):
                if name in names or not (base / name).is_file():
                    continue
                raw = (base / name).read_bytes()
                entry = tarfile.TarInfo(name)
                entry.size = len(raw)
                entry.mode = 0o644
                entry.mtime = 0
                new.addfile(entry, io.BytesIO(raw))
                added.append(name)
shutil.copyfile(temporary, archive)
print('Added exact pinned root sources:', ', '.join(added))
print('Source archive bytes:', archive.stat().st_size)
