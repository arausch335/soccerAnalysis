"""Run WhoScored-only event scraping directly from an IDE or terminal."""
import sys
import argparse
import config

from collection.whoScoredEvents import main as collect


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if '--help' in args or '-h' in args:
        return collect(args)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--competition')
    parser.add_argument('--season')
    selection,forwarded = parser.parse_known_args(args)
    result = 0
    for competition in ([selection.competition] if selection.competition else config.COMPETITIONS):
        for season in ([selection.season] if selection.season else config.SEASONS):
            options = ['--competition',competition,'--season',season,*forwarded]
            code = collect(options)
            if code == 130:
                return code
            result = max(result,code)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
