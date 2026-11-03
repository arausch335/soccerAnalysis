"""Public dataset paths and private, rebuildable linker inputs."""
from pathlib import Path
import shutil
import re


def public_root(dataset):
    path = Path(dataset)
    if path.parts[-3:] == ('concordance', 'metadata', 'compiled'):
        return path.parents[2]
    return path


def compiled_root(dataset):
    return public_root(dataset) / 'concordance' / 'metadata' / 'compiled'


def prepare(dataset):
    """Seed private linker inputs from older layouts without removing old exports."""
    public = public_root(dataset)
    target = compiled_root(public)
    target.mkdir(parents=True, exist_ok=True)
    for relative in ('fixtures.csv', 'player_ids.csv', 'team_ids.csv', 'match_ids.csv', 'fixture_concordance'):
        old, new = public / relative, target / relative
        if old.is_file() and not new.exists():
            shutil.copy2(old, new)
        elif old.is_dir() and not new.exists():
            shutil.copytree(old, new)
    # Computers holding exports without raw checkpoints can retain their season CSVs too.
    from collection.storageTransfer import link_or_copy
    for season in public.glob('*/*'):
        if not season.is_dir() or not re.fullmatch(r'\d{4}-\d{4}',season.name):continue
        if season.parent.name in ('raw_data','concordance','concordances'):continue
        for name in ('fixtures.csv','player_match_stats.csv','team_match_stats.csv'):
            source=season/name
            destination=public/'raw_data'/season.parent.name/season.name/name
            if source.is_file() and not destination.exists():
                destination.parent.mkdir(parents=True,exist_ok=True)
                link_or_copy(source,destination)
    # Historical detailed concordances are inputs, not the public CSV tables.
    for relative in ('full', 'scraped', 'bundle.json', 'team_history.csv', 'alex_registry.json',
                     'id_redirects.csv', 'registry_conflicts.csv', 'quarantine.csv', 'reconciliation'):
        old, new = public / 'concordance' / relative, target / 'concordance' / relative
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            if old.is_dir(): shutil.copytree(old, new)
            else: shutil.copy2(old, new)
    if (public/'concordances/manifest.json').is_file() and not (target/'fixtures.csv').is_file():
        import pandas as pd
        from collection.files import atomic_csv
        from storage.pandasStore import atomic_json
        from . import legacy_parquet
        legacy_parquet.validate(public/'concordances')
        atomic_csv(pd.DataFrame(legacy_parquet.wide_rows(public)),target/'fixtures.csv')
        for kind in ('match','team','player'):
            atomic_csv(pd.DataFrame(legacy_parquet.reference_rows(public,kind)),target/(kind+'_ids.csv'))
        atomic_json(target/'fixture_concordance/alex_registry.json',legacy_parquet.read_registry(public/'concordances'))
    return target


def retire_legacy(dataset):
    """Archive old exports only after the replacement publication is validated."""
    public = public_root(dataset)
    archive = public / 'concordance' / 'metadata' / 'legacy'
    names = ['fixtures.csv', 'player_ids.csv', 'team_ids.csv', 'match_ids.csv',
             'fixture_concordance', 'concordances']
    for path in public.iterdir():
        if path.is_dir() and path.name not in ('raw_data', 'concordance') and any(path.glob('????-????/*.csv')):
            names.append(path.name)
    for name in names:
        source = public / name
        if source.exists():
            archive.mkdir(parents=True, exist_ok=True)
            destination = archive / name
            if destination.exists():
                # An older archived copy must not be overwritten.
                from uuid import uuid4
                destination = archive / (name + '.' + uuid4().hex)
            shutil.move(str(source), str(destination))
