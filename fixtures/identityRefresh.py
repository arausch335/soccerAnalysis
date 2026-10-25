"""Rejoin compiled identity exports to the current Reep index without scraping.

Published bridges supply IDs, not proof that a provider's match data exists.
Existing disagreements remain review items; native IDs are never overwritten.
"""
from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from filelock import FileLock
import pandas as pd

from collection.files import atomic_csv
from providers.reep import PROVIDERS, index, snapshot
from storage.pandasStore import atomic_json

SOURCES = (*PROVIDERS, 'reep')


def read_table(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or ()), list(reader)


def coverage(rows, suffix='_id'):
    return dict(rows=len(rows), present={p:sum(bool(r.get(p+suffix)) for r in rows) for p in SOURCES},
                conflicts=sum(r.get('identity_status',r.get('match_identity_status'))=='conflict' for r in rows))


def resolve_rows(rows, bridges, kind, *, suffix='_id', status='identity_status'):
    """Only fill unique, nonconflicting bridges; enforce one-to-one match IDs."""
    proposed, review = [], []
    for row in rows:
        if row.get(status)=='conflict':
            proposed.append({})
            review.append(dict(kind=kind,sofascore_id=row.get('sofascore'+suffix,''),
                               reason='existing_identity_conflict',provider='',candidate_id=''))
            continue
        result=bridges.resolve(kind,{p+'_id':row.get(p+suffix,'') for p in PROVIDERS})
        if result['identity_status']=='conflict':
            proposed.append({'identity_status':'conflict'})
            review.append(dict(kind=kind,sofascore_id=row.get('sofascore'+suffix,''),
                               reason='published_bridge_conflict',provider='',candidate_id=''))
        else:
            proposed.append(result)
    claims=defaultdict(set)
    if kind=='match':
        for n,(row,result) in enumerate(zip(rows,proposed)):
            for p in SOURCES:
                native=row.get(p+suffix) or result.get(p+'_id')
                if native:claims[p,native].add(n)
    output=[]
    for n,(row,result) in enumerate(zip(rows,proposed)):
        row=dict(row)
        if result.get('identity_status')=='conflict':
            row[status]='conflict';output.append(row);continue
        collisions=[p for p in SOURCES if kind=='match' and not row.get(p+suffix)
                    and result.get(p+'_id') and len(claims[p,result[p+'_id']])!=1]
        if collisions:
            for p in collisions:
                review.append(dict(kind=kind,sofascore_id=row.get('sofascore'+suffix,''),
                                   reason='provider_fixture_reused',provider=p,candidate_id=result[p+'_id']))
            row[status]='conflict';output.append(row);continue
        old=row.get('reep'+suffix)
        if old and result.get('reep_id') and old!=result['reep_id']:
            if bridges.canonical_id(old)==result['reep_id']:
                row['reep'+suffix]=result['reep_id']
            else:
                review.append(dict(kind=kind,sofascore_id=row.get('sofascore'+suffix,''),
                                   reason='canonical_id_disagreement',provider='reep',candidate_id=result['reep_id']))
                row[status]='conflict';output.append(row);continue
        for p in SOURCES:
            key=p+suffix; candidate=result.get(p+'_id')
            if row.get(key) or not candidate:continue
            if suffix=='_match_id' and p in PROVIDERS[1:]:
                if row.get(p+'_fixture_check','missing') not in ('','missing','bridge_only'):
                    continue
                row[p+'_fixture_check']='bridge_only'
                row[p+'_match_method']='reep_bridge'
            row[key]=candidate
        if result and result.get('reep_id')==row.get('reep'+suffix):
            row[status]=result['identity_status']
        output.append(row)
    return output,review


def reconcile_exports(root, dataset, *, dry_run=False, already_locked=False):
    """Update received exports and coverage, retaining one previous-file backup.

    No provider requests or raw-data scans. The receiver calls this while it
    owns its install lock, so a worker cannot replace files during reconciliation.
    """
    root,dataset=Path(root),Path(dataset)
    if snapshot(root) is None:return {'state':'no_index','tables':{}}
    with ExitStack() as stack:
        if not already_locked:
            lock=root/'_system/compiled/receiver/.install.lock'
            lock.parent.mkdir(parents=True,exist_ok=True)
            stack.enter_context(FileLock(str(lock),timeout=25))
        stack.enter_context(FileLock(str(dataset/'fixtures.csv')+'.lock',timeout=25))
        return _reconcile(root,dataset,dry_run=dry_run)


def _reconcile(root,dataset,*,dry_run):
    bridges=index(root); pending={}; report={'state':'dry_run' if dry_run else 'updated',
        'at':datetime.now(timezone.utc).isoformat(),'release':bridges.metadata.get('release'), 'tables':{}}
    review=[]; fixture_rows=None;removed_ids=set();attempts=[];score_repairs=[]
    previous=dataset/'concordance/reconciliation/review.csv'
    retry_ids=set()
    if previous.exists():
        _,prior=read_table(previous)
        retry_ids={r['sofascore_id'] for r in prior if r.get('kind')=='match' and r.get('reason')=='provider_fixture_reused'}
    path=dataset/'fixtures.csv'
    if path.exists():
        columns,rows=read_table(path)
        if {'sofascore_match_id','home_team','away_team'}<=set(columns):
            from .reconcile import prune_exported_fixtures
            from .scoreRepair import repair_exported_scores
            repaired,score_repairs=repair_exported_scores(rows,root,bridges)
            retained,attempts=prune_exported_fixtures(repaired,bridges)
            removed_ids={r['sofascore_match_id'] for r in rows}-{r['sofascore_match_id'] for r in retained}
            # Re-evaluate only machine-generated collision flags. Human/source
            # identity disagreements stay blocked until independently resolved.
            retry=[{**r,'match_identity_status':'unmapped'} if r['sofascore_match_id'] in retry_ids else r for r in retained]
            new,issues=resolve_rows(retry,bridges,'match',suffix='_match_id',status='match_identity_status')
            review.extend(issues)
            for side in ('home','away'):
                new,issues=resolve_rows(new,bridges,'team',suffix='_'+side+'_team_id',status=side+'_team_identity_status')
                review.extend(issues)
            fixture_rows=new
            report['tables']['fixtures']={'before':coverage(rows,'_match_id'),'after':coverage(new,'_match_id')}
            if new!=rows:pending[path]=(columns,new)
    for folder in ('concordance','fixture_concordance'):
        for kind in ('player','team','match'):
            path=dataset/folder/(kind+'_ids.csv')
            if not path.exists():continue
            columns,rows=read_table(path)
            if not {'sofascore_id','fotmob_id','whoscored_id'}<=set(columns):continue
            if folder=='fixture_concordance' and kind=='match' and fixture_rows is not None:
                new=[dict(competition=r.get('competition',''),season=r.get('season',''),date=r.get('date',''),
                          home_team_name=r['home_team'],away_team_name=r['away_team'],
                          **{p+'_id':r.get(p+'_match_id','') for p in SOURCES},identity_status=r['match_identity_status']) for r in fixture_rows]
            else:
                retained=[r for r in rows if r['sofascore_id'] not in removed_ids] if kind=='match' else rows
                new,issues=resolve_rows(retained,bridges,kind);review.extend(issues)
            report['tables'][folder+'/'+kind]={'before':coverage(rows),'after':coverage(new)}
            if new!=rows:pending[path]=(columns,new)
    # Keep the legacy native-only match list consistent with the concordance.
    path=dataset/'match_ids.csv'
    if removed_ids and path.exists():
        columns,rows=read_table(path)
        retained=[r for r in rows if r.get('sofascore_id') not in removed_ids]
        if retained!=rows:pending[path]=(columns,retained)
    if fixture_rows is not None:
        by_id={r['sofascore_match_id']:r for r in fixture_rows}
        path=dataset/'fixture_concordance/issues.csv'
        if path.exists():
            columns,records=read_table(path)
            kept=[r for r in records if r.get('sofascore_match_id') not in removed_ids
                  and not (r.get('issue')=='reep_fixture_failed_validation'
                           and by_id.get(r.get('sofascore_match_id'),{}).get(r.get('provider','')+'_fixture_check')=='validated')]
            if kept!=records:pending[path]=(columns,kept)
        path=dataset/'fixture_concordance/coverage.csv'
        if path.exists():
            columns,records=read_table(path);groups=defaultdict(list)
            for row in fixture_rows:groups[row.get('competition','').removesuffix(' Qualification'),row.get('season','')].append(row)
            new=[]
            for record in records:
                row=dict(record);subset=groups[row.get('competition'),row.get('season')];provider=row.get('provider')
                if provider=='sofascore':
                    row['canonical_fixtures']=str(len(subset));row['finished_fixtures']=str(sum(r.get('status')=='finished' for r in subset))
                row['matched_fixtures']=str(len(subset) if provider=='sofascore' else sum(r.get(provider+'_fixture_check')=='validated' for r in subset))
                row['unmatched_fixtures']=str(len(subset)-int(row['matched_fixtures']))
                new.append(row)
            columns=list(dict.fromkeys([*columns,'canonical_fixtures','finished_fixtures','matched_fixtures','unmatched_fixtures']))
            if new!=records:pending[path]=(columns,new)
    manifests={}
    for folder in ('concordance','fixture_concordance'):
        path=dataset/folder/'manifest.json'
        if not path.exists():continue
        value=json.loads(path.read_text(encoding='utf-8'));value['reep']=bridges.metadata
        value['identity_refreshed_at']=report['at']
        if folder=='concordance':
            for kind in ('player','team','match'):
                detail=report['tables'].get(folder+'/'+kind)
                if detail:
                    after=detail['after'];n=after['rows']
                    value.setdefault('tables',{})[kind]={'rows':n,'reep_linked':after['present']['reep'],
                        'conflicts':after['conflicts'],'missing_ids':{p:n-after['present'][p] for p in PROVIDERS}}
        elif fixture_rows is not None:
            value['rows']=len(fixture_rows)
            value['superseded_fixtures_removed']=value.get('superseded_fixtures_removed',0)+len(removed_ids)
            issues_path=dataset/'fixture_concordance/issues.csv'
            if issues_path in pending:value['issues']=len(pending[issues_path][1])
            value['missing_ids']={c:sum(not r.get(c) for r in fixture_rows) for c in fixture_rows[0] if any(c==p+'_'+e+'_id' for p in SOURCES for e in ('match','home_team','away_team'))} if fixture_rows else {}
            value['all_four_ids_complete']=bool(fixture_rows) and not any(value['missing_ids'].values())
            value['fixture_checks']={p:dict(Counter(r.get(p+'_fixture_check','missing') for r in fixture_rows)) for p in PROVIDERS[1:]}
            value['all_provider_fixtures_validated']=bool(fixture_rows) and all(r.get(p+'_fixture_check')=='validated' for r in fixture_rows for p in PROVIDERS[1:])
            # Preserve schedule completeness: an ID bridge does not repair a
            # failed season request or prove that event/player data exists.
        manifests[path]=value
    review=list({tuple(sorted(row.items())):row for row in review}.values())
    report['review_items']=len(review)
    report['changed_files']=len(pending)
    report['superseded_fixtures_removed']=len(removed_ids)
    report['scorelines_repaired']=len(score_repairs)
    if dry_run:return report
    backup=root/'_system/identity/previous_exports'
    for path,(columns,rows) in pending.items():
        target=backup/path.relative_to(dataset);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
        atomic_csv(pd.DataFrame(rows,columns=columns),path)
    for path,value in manifests.items():
        target=backup/path.relative_to(dataset);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target);atomic_json(path,value)
    if attempts:
        path=dataset/'fixture_concordance/attempts.csv'
        from .reconcile import ATTEMPT_COLUMNS
        records=read_table(path)[1] if path.exists() else []
        atomic_csv(pd.DataFrame([*records,*attempts],columns=ATTEMPT_COLUMNS).drop_duplicates(),path)
    destination=dataset/'concordance/reconciliation'
    atomic_csv(pd.DataFrame(review,columns=['kind','sofascore_id','reason','provider','candidate_id']).drop_duplicates(),destination/'review.csv')
    atomic_json(destination/'summary.json',report)
    if score_repairs:
        atomic_csv(pd.DataFrame(score_repairs),destination/'score_repairs.csv')
    return report
