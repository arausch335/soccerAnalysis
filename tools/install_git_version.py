"""Install the committed Git version without deleting existing local files."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import tempfile

REPOSITORY = 'https://github.com/arausch335/soccerAnalysis.git'
BRANCH = 'simplify-dataset-workflow'
COMMIT = '90c5fca763ef234750b4b58c03724a07a6579ae0'
PRESERVE = {'.venv', 'venv', 'env', '.idea', '.vscode', 'data', 'dataset',
            'datasets', 'output', 'outputs', 'logs', 'secrets', 'credentials'}


def install(project, stage, backup):
    """Move old source to an archive; restore it if installation fails."""
    if not project.is_dir():
        raise ValueError(f'Project directory does not exist: {project}')
    if (project / '.git').exists():
        raise ValueError('This folder already has Git. Stop and update that checkout instead.')
    if not (stage / '.git').is_dir() or not (stage / 'dataset_builder.py').is_file():
        raise ValueError('The staged clone is not the reorganized project.')
    incoming = list(stage.iterdir())
    kept = [p for p in project.iterdir()
            if p.name in PRESERVE or p.name == '.env' or p.name.startswith('.env.')]
    conflicts = {p.name for p in kept} & {p.name for p in incoming}
    if conflicts:
        raise ValueError('Clone would replace preserved files: ' + ', '.join(sorted(conflicts)))
    old = [p for p in project.iterdir() if p not in kept]
    backup.mkdir()  # Refuses to overwrite an existing archive.
    moved_old, moved_new = [], []
    try:
        for path in old:
            path.rename(backup / path.name)
            moved_old.append(path.name)
        for path in incoming:
            path.rename(project / path.name)
            moved_new.append(path.name)
        # The earlier Git transfer omitted local provider reference JSON files.
        # Retain these in the archive and copy them into the renamed adapters.
        old_api = backup / 'api'
        if old_api.is_dir():
            for source in old_api.glob('*.json'):
                target = project / 'providers' / source.name
                if not target.exists():
                    shutil.copy2(source, target)
        # Keep local-only reference snapshots out of future Git commits.
        exclude = project / '.git/info/exclude'
        with exclude.open('a') as handle:
            handle.write('\n# Preserved local provider reference snapshots\n/providers/*.json\n')
    except BaseException:
        # All names are retained: return the new checkout to staging, then
        # restore the old project files from their archive.
        for name in reversed(moved_new):
            (project / name).rename(stage / name)
        for name in reversed(moved_old):
            (backup / name).rename(project / name)
        raise
    print(f'Installed Git branch: {BRANCH}')
    print(f'Project: {project}')
    print(f'Old code archive: {backup}')
    print('Existing environment, editor settings, and data folders stayed in place.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path,
                        default=Path.home() / 'PycharmProjects' / 'soccerAnalysis')
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    if not project.is_dir() or (project / '.git').exists():
        parser.error('Use the existing non-Git project folder. No files were changed.')
    stage = Path(tempfile.mkdtemp(prefix='soccerAnalysis-git-staging-', dir=project.parent))
    subprocess.run(['git', 'clone', '--branch', BRANCH, '--single-branch',
                    REPOSITORY, str(stage)], check=True)
    subprocess.run(['git', '-C', str(stage), 'checkout', BRANCH], check=True)
    actual = subprocess.check_output(['git', '-C', str(stage), 'rev-parse', 'HEAD'],
                                     text=True).strip()
    verified = subprocess.run(['git', '-C', str(stage), 'merge-base', '--is-ancestor',
                               COMMIT, actual], check=False)
    if verified.returncode:
        parser.error(f'The branch does not contain the verified project changes. Clone retained at {stage}; original project unchanged.')
    backup = project.with_name(project.name + '-before-git-' +
                               datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    install(project, stage, backup)
    subprocess.run(['git', '-C', str(project), 'status', '--short', '--branch'], check=True)


if __name__ == '__main__':
    main()
