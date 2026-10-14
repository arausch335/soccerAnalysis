"""Build player maps and match replay from local enriched events.

Press Run in PyCharm to build and open the explorer. Use --no-open to only build.
Other arguments are forwarded to match_visualization.build (see --help).
"""
from pathlib import Path
import sys
import webbrowser
from analysis.match_visualization.build import main as build


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    open_browser='--no-open' not in args
    args=[a for a in args if a!='--no-open']
    output=build(args).resolve()
    print(f'Explorer ready: {output}')
    if open_browser:
        webbrowser.open(output.as_uri())
    return 0


if __name__=='__main__':
    raise SystemExit(main())
