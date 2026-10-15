"""Run WhoScored-only event scraping directly from an IDE or terminal."""
import sys

from collection.whoScoredEvents import main as collect


COMPETITIONS = ['Premier League']
SEASONS = ['2024/25']
REFRESH_SCHEDULE = False  # True adds newly finished fixtures to saved schedules.


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        return collect(args)
    result = 0
    for competition in COMPETITIONS:
        for season in SEASONS:
            options = ['--competition',competition,'--season',season]
            if REFRESH_SCHEDULE:
                options.append('--refresh-schedule')
            code = collect(options)
            if code == 130:
                return code
            result = max(result,code)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
