"""Prepare a Chrome-loadable folder containing only extension runtime assets."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

ASSETS = ('manifest.json', 'worker.js', 'page.js', 'who_page.js', 'fotmob_page.js', 'protocol.js',
          'popup.html', 'popup.js', 'popup.css')
MARKER = 'collector-build.json'
FORMAT = 'soccer-collector-extension'


def prepare_extension(output, *, source=None):
    # This also works beside tools/ in an installed wheel. Do not import the
    # assets as Python: Chrome reserves __init__.py and __pycache__ names.
    source = Path(source) if source is not None else Path(__file__).resolve().parents[1] / 'chrome_extension'
    source = source.resolve()
    output = Path(output).expanduser()
    if output.is_symlink():
        raise ValueError('Choose a real output folder, not a symbolic link')
    output = output.resolve()
    if output == source or output in source.parents or source in output.parents:
        raise ValueError('The prepared folder must be separate from the source extension folder')
    if output.exists() and not output.is_dir():
        raise ValueError('Output must be a folder')
    if output.exists() and any(output.iterdir()):
        try:
            owned = json.loads((output / MARKER).read_text(encoding='utf-8')).get('format') == FORMAT
        except (OSError, ValueError, AttributeError):
            owned = False
        if not owned:
            raise ValueError('Output contains unrelated files; choose an empty folder')
    payloads = {name: (source / name).read_bytes() for name in ASSETS}
    manifest = json.loads(payloads['manifest.json'])
    if (manifest.get('manifest_version') != 3 or
            manifest.get('background', {}).get('service_worker') not in ASSETS or
            manifest.get('action', {}).get('default_popup') not in ASSETS):
        raise ValueError('Extension manifest references missing runtime assets')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='soccer-extension-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'extension'
        staged.mkdir()
        for name, data in payloads.items():
            (staged / name).write_bytes(data)
        (staged / MARKER).write_text(json.dumps({'format': FORMAT, 'version': 1}), encoding='utf-8')
        if output.exists():
            shutil.rmtree(output)
        staged.rename(output)
    return output


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.cwd() / 'build' / 'soccer-collector-extension')
    args=parser.parse_args(argv)
    try:
        directory=prepare_extension(args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print('Prepared Soccer Collector extension folder:')
    print(directory)
    print('\nIn Chrome, open chrome://extensions, enable Developer mode, click Load unpacked, and select that folder.')
    print('Open a Sofascore, WhoScored or FotMob page in normal Chrome. In Python, run main.py with:')
    print('--collection-types combined --competition "Premier League" --season 2024/25 --limit 1')
    print('Paste the connection code printed by Python into the extension, then click Connect this tab.')
    return 0


if __name__=='__main__':raise SystemExit(main())
