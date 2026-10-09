"""Rebuild saved-season CSVs and optional combined ID tables.

Edit the settings below before running this file. This script deliberately has
no command-line flags so a build is reproducible from the Python configuration.
It stays offline unless REFRESH_PUBLISHED_TEAM_IDS is enabled.
"""
import sys
from pathlib import Path

from collect import main as collect
from dataset_manager import (build_id_tables, package_collection,
                             standardize_fixture_team_names)
from api.team_crosswalk import refresh_team_crosswalk
from collection.paths import collection_directory, collection_name, discover_collections


# Python build settings. Leave COLLECTION_NAMES empty to rebuild every saved
# competition-season collection.
PROJECT_ROOT=Path(__file__).resolve().parent
DATA_ROOT=PROJECT_ROOT/'data'
DATASET_ROOT=PROJECT_ROOT/'dataset'
COLLECTION_NAMES=()
COMPILE_EVENTS=False
REPLACE_DATASET=False
# True skips every per-season export/package step and only rebuilds the
# top-level reference tables selected by the three settings below.
REFERENCE_CSVS_ONLY=True
BUILD_PLAYER_IDS_CSV=True
# Unknown SofaScore/WhoScored team IDs remain NA; the build prints missing-ID counts.
BUILD_TEAM_IDS_CSV=True
BUILD_MATCH_IDS_CSV=True
# Refresh the published provider-ID snapshot when desired. This downloads the
# Reep register; the normal reference build uses the saved JSON snapshot.
REFRESH_PUBLISHED_TEAM_IDS=False
# Set True only after team_ids.csv has been rebuilt successfully. This rewrites
# dataset/fixtures.csv display names using FotMob IDs; match IDs and scores stay put.
STANDARDIZE_FIXTURE_TEAM_NAMES=False


def _build_reference_csvs(root,dataset_root):
    if not BUILD_PLAYER_IDS_CSV and not BUILD_TEAM_IDS_CSV and not BUILD_MATCH_IDS_CSV:
        raise ValueError('Enable at least one BUILD_*_IDS_CSV setting')
    if STANDARDIZE_FIXTURE_TEAM_NAMES and not BUILD_TEAM_IDS_CSV:
        raise ValueError('Enable BUILD_TEAM_IDS_CSV before standardizing fixtures')
    try:
        if REFRESH_PUBLISHED_TEAM_IDS and BUILD_TEAM_IDS_CSV:
            count=refresh_team_crosswalk(dataset_root/'fixtures.csv',
                                          PROJECT_ROOT/'api'/'team_id_crosswalk.json')
            print(f'Checked published provider links for {count} fixture teams.',
                  flush=True)
        written=build_id_tables(root,dataset_root,
            build_player_ids=BUILD_PLAYER_IDS_CSV,
            build_team_ids=BUILD_TEAM_IDS_CSV,
            build_match_ids=BUILD_MATCH_IDS_CSV)
        for label,path in written.items():print(f'Wrote combined {label}: {path}',flush=True)
        if STANDARDIZE_FIXTURE_TEAM_NAMES:
            standardize_fixture_team_names(dataset_root/'fixtures.csv',
                                           dataset_root/'team_ids.csv')
            print(f'Standardized fixture team names: {dataset_root/"fixtures.csv"}',flush=True)
        return 0
    except (OSError,ValueError,KeyError) as exc:
        print(f'Failed to build combined ID CSVs: {exc}',flush=True)
        return 2


def main():
    root=Path(DATA_ROOT).resolve();dataset_root=Path(DATASET_ROOT).resolve()
    if REFERENCE_CSVS_ONLY:
        print('Building reference CSVs only; saved-season exports are skipped.',flush=True)
        return _build_reference_csvs(root,dataset_root)
    if REPLACE_DATASET and not COMPILE_EVENTS:
        raise ValueError('REPLACE_DATASET requires COMPILE_EVENTS so events.csv cannot be stale')
    names=(list(COLLECTION_NAMES) or
           [collection_name(path) for path in discover_collections(root,'job.json')])
    if not names:raise ValueError(f'No saved seasons found under {root}')
    missing=[name for name in names if not (collection_directory(root,name)/'job.json').exists()]
    if missing:raise ValueError('Unknown saved season(s): '+', '.join(missing))
    results=[]
    for index,name in enumerate(names,1):
        print(f'\n=== EXPORT {index}/{len(names)}: {name} ===',flush=True)
        command=['export','--data-root',str(root),'--name',name,
                 '--compile-events' if COMPILE_EVENTS else '--no-compile-events']
        code=collect(command)
        if code==0 and REPLACE_DATASET:
            try:
                destination=package_collection(
                    collection_directory(root,name),dataset_root,replace=True)
                print(f'Replaced packaged dataset: {destination}',flush=True)
            except (OSError,ValueError,KeyError) as exc:
                print(f'Failed to replace packaged dataset for {name}: {exc}',flush=True)
                code=2
        results.append(code)
        print(f'=== FINISHED {index}/{len(names)}: {name} (exit {code}) ===',flush=True)
    if all(code==0 for code in results) and (BUILD_PLAYER_IDS_CSV or BUILD_TEAM_IDS_CSV or
                                              BUILD_MATCH_IDS_CSV):
        results.append(_build_reference_csvs(root,dataset_root))
    return 0 if all(code==0 for code in results) else 2


if __name__=='__main__':
    if len(sys.argv)!=1:
        raise SystemExit('build_csvs.py has no CLI flags; edit its Python build settings instead')
    raise SystemExit(main())
