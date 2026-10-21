"""Refresh the compact Reep index, then rebuild dataset/concordance ID exports.

Run on Main: python -m tools.refresh_reep
Workers receive this index automatically through direct reporting.
"""
import config
from providers.reep import refresh
from dataset_builder import build_id_tables


def main():
    value = refresh(config.DATA_ROOT, config.DATASET_ROOT, config.PROJECT_ROOT / 'ensemble_data')
    print(f"Reep {value['release']}: retained {len(value['rows'])} typed bridges.")
    build_id_tables(config.DATA_ROOT, config.DATASET_ROOT)


if __name__ == '__main__':
    main()
