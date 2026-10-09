"""Transfer cached collection state separately from Git, with checksum checks."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile
from filelock import FileLock


def pack(root, archive):
    root, archive = Path(root).resolve(), Path(archive).resolve()
    if not (root/'.soccer-scraper.json').exists():
        raise ValueError('Not a scraper data directory')
    if archive.is_relative_to(root):
        raise ValueError('Place the archive outside the data directory')
    with FileLock(str(root/'.collector.lock'),timeout=0), zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        manifest = {}
        for path in sorted(root.rglob('*')):
            if path.is_symlink(): raise ValueError('Symlinks cannot be transferred')
            if not path.is_file() or path.name.endswith('.lock'): continue
            name = path.relative_to(root).as_posix()
            content = path.read_bytes()
            manifest[name] = hashlib.sha256(content).hexdigest()
            z.writestr(name,content)
        z.writestr('TRANSFER_MANIFEST.json',json.dumps(manifest,sort_keys=True))
    return archive


def restore(archive, target):
    target = Path(target).resolve()
    if target.exists(): raise ValueError('Restore target must not exist; existing data is never replaced')
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('TRANSFER_MANIFEST.json'))
        if '.soccer-scraper.json' not in manifest: raise ValueError('Missing data marker')
        if len(z.namelist()) != len(set(z.namelist())): raise ValueError('Duplicate archive members')
        if set(z.namelist()) != set(manifest)|{'TRANSFER_MANIFEST.json'}: raise ValueError('Manifest mismatch')
        with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
            staging = Path(tmp)/'data'
            staging.mkdir()
            for name, digest in manifest.items():
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                    raise ValueError('Unsafe archive path')
                content = z.read(name)
                if hashlib.sha256(content).hexdigest() != digest: raise ValueError('Checksum mismatch: '+name)
                output = staging.joinpath(*path.parts)
                output.parent.mkdir(parents=True,exist_ok=True)
                output.write_bytes(content)
            staging.rename(target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['pack','restore'])
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    args = parser.parse_args()
    print((pack if args.command=='pack' else restore)(args.source,args.destination))
