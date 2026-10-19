"""Run WhoScored with the same imports, worker inventories and actions as main.py."""
import sys
from main import main as collect


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    return collect([*args,'--collection-types','whoscored_events'])


if __name__=='__main__':raise SystemExit(main())
