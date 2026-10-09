"""Run the local role-analysis pipeline. Press Run in PyCharm; no collection requests.

Outputs include match roles, season profiles, within-position comparisons,
exploratory clustering diagnostics and an interactive report.
"""
import argparse
from pathlib import Path
import webbrowser
from role_pipeline import run,Config

ROOT=Path(__file__).resolve().parent

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',type=Path,default=ROOT)
    p.add_argument('--data-dir',type=Path);p.add_argument('--event-dir',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--competition',default='Premier League');p.add_argument('--min-minutes',type=int,default=450)
    p.add_argument('--min-matches',type=int,default=5);p.add_argument('--bootstrap-runs',type=int,default=20)
    p.add_argument('--no-open',action='store_true')
    a=p.parse_args(argv)
    if a.min_minutes<=0 or a.min_matches<2 or a.bootstrap_runs<1:p.error('Use positive minutes, at least 2 matches, and at least 1 bootstrap run.')
    root=a.project_root.resolve();data=a.data_dir or root/'data/premier-league/2024-2025'
    output=a.output or root/'outputs/role-analysis'
    result=run(root,data,a.event_dir or root/'outputs/event-layer-full',output,
        Config(min_minutes=a.min_minutes,min_matches=a.min_matches,bootstrap_runs=a.bootstrap_runs),a.competition)
    if not a.no_open:webbrowser.open((output/'role-explorer.html').resolve().as_uri())
    return 0

if __name__=='__main__':raise SystemExit(main())
