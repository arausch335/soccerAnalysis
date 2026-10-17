"""Visible-browser Sofascore collection for the selections in settings.yaml."""
import sys
from main import main as collect


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if '--help' in args or '-h' in args:
        from collection.cli import main as cli
        return cli(['--help'])
    # The standalone browser run requests all spatial categories and Sofascore
    # only. The normal main.py workflow keeps its configured enrichment choices.
    if '--sofascore-transport' not in args:
        args.extend(['--sofascore-transport','browser'])
    if '--providers' not in args:args.extend(['--providers','sofascore'])
    if '--sofascore-spatial-capture' not in args and '--no-spatial' not in args:
        args.extend(['--sofascore-spatial-capture','all'])
    args.extend(['--collection-types','sofascore'])
    return collect(args)


if __name__=='__main__':raise SystemExit(main())
