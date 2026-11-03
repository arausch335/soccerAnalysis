"""Compatibility command for the shared CSV dataset builder."""

def main(argv=None):
    from dataset_builder import main as build
    return build(argv)


if __name__ == '__main__':
    raise SystemExit(main())
