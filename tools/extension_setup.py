"""Print the extension folder and the first-match setup steps."""
from pathlib import Path
import chrome_extension


def main():
    directory=Path(chrome_extension.__file__).parent.resolve()
    print('Soccer Collector extension folder:')
    print(directory)
    print('\nIn Chrome, open chrome://extensions, enable Developer mode, click Load unpacked, and select that folder.')
    print('Open a Sofascore match in normal Chrome. In Python, run main.py with:')
    print('--collection-types sofascore --sofascore-transport extension --competition "Premier League" --season 2024/25 --limit 1')
    print('Paste the connection code printed by Python into the extension, then click Connect this Sofascore tab.')
    return 0


if __name__=='__main__':raise SystemExit(main())
