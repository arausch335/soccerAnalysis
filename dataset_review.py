"""Coverage of packaged local files, independent of worker inventory claims."""
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import csv
import json

from storage.pandasStore import atomic_json

FORMAT='soccer-dataset-coverage-v1'
TABLES=('player_match_stats','team_match_stats','fixtures')


def _files(dataset):
    return sorted(p for name in TABLES for p in (dataset/'raw_data').glob('*/*/'+name+'.csv'))


def _inventory(dataset):
    return [[p.relative_to(dataset).as_posix(),p.stat().st_size] for p in _files(dataset)]


def _review_rows(path):
    import pandas as pd
    with path.open(encoding='utf-8-sig',newline='') as stream:
        fields=next(csv.reader(stream),[])
    if 'match_id' not in fields:raise ValueError('Packaged table lacks match_id: '+str(path))
    selected={'match_id','competition','season','provider','minutes_played','goals','xg','xa','touches',
              'player_id','player_name','fotmob_player_stats_status','fotmob_statistic_group_conflicts'}
    for chunk in pd.read_csv(path,dtype=str,keep_default_na=False,chunksize=50_000,
                             usecols=lambda name:name in selected,encoding='utf-8-sig'):
        yield from chunk.to_dict('records')


def create_review(dataset,*,deferred_seasons=(),package_diagnostic=None):
    from concordance.publication import folder,manifest_path,digest,read_table
    dataset=Path(dataset);base=folder(dataset)
    manifest=json.loads(manifest_path(base).read_text())
    before=[(p.relative_to(dataset).as_posix(),p.stat().st_size,p.stat().st_mtime_ns) for p in _files(dataset)]
    before_manifest=digest(manifest_path(base))
    core=read_table(base,'fixtures/fixtures').fillna('')
    facts=read_table(base,'fixtures/metadata').fillna('')
    groups={};entity_group={};lookup={};native=defaultdict(set)
    for row in core.to_dict('records'):
        key=(str(row['competition']),str(row['season']))
        group=groups.setdefault(key,{'competition':key[0],'season':key[1],'fixtures':0,'saved_stats_evidence':0,
                                    'player_match_stats':{'rows':0,'matches':0},'team_match_stats':{'rows':0,'matches':0},
                                    'unlinked_matches':0})
        group['fixtures']+=1;group['saved_stats_evidence']+=str(row.get('has_player_stats')).lower()=='true'
        entity_group[str(row['match_entity_id'])]=key
    for row in facts.to_dict('records'):
        entity=str(row['match_entity_id']);key=entity_group.get(entity)
        if key and row.get('match_id'):lookup[(*key,str(row['match_id']))]=entity
    ids={}
    for kind in ('match','team','player'):
        frame=read_table(base,kind+'_ids').fillna('');records=frame.to_dict('records')
        ids[kind]={'total':len(records),'all_three':sum(all(r.get(p+'_id') for p in ('sofascore','fotmob','whoscored')) for r in records),
                   'providers':{p:sum(bool(r.get(p+'_id')) for r in records) for p in ('sofascore','fotmob','whoscored')}}
        if kind=='match':
            for row in records:
                for p in ('sofascore','fotmob','whoscored'):
                    if row.get(p+'_id'):native[str(row[p+'_id'])].add(str(row['entity_id']))
    team_metadata=read_table(base,'metadata/team_ids').fillna('')
    bridge_aliases=sum(len(values) for row in team_metadata.to_dict('records')
        for values in json.loads(row.get('provider_aliases') or '{}').values())
    mapping_conflicts=[{'entity_id':str(row['entity_id']),'details':str(row['reviewed_mapping_conflicts'])}
        for row in team_metadata.to_dict('records') if row.get('reviewed_mapping_conflicts')]
    # Collection calls these Qualifiers; the catalogue uses Qualification.
    # Resolve the reviewed naming aliases before using provider IDs to link.
    def competition_key(name):
        for league in ('Champions League','Europa League','Conference League'):
            if name in (league+' Qualifiers',league+' Qualification'):return league+' Qualification'
        return name
    group_aliases=defaultdict(set)
    for key in groups:group_aliases[competition_key(key[0]),key[1]].add(key)
    matches=defaultdict(set);unlinked=defaultdict(set);files=[];quality=Counter()
    conflict_examples=[]
    for path in _files(dataset):
        table=path.stem;rows=0;observed=set();fields=Counter();providers=Counter()
        for row in _review_rows(path):
            rows+=1;mid=str(row.get('match_id') or '')
            if row.get('fotmob_player_stats_status')=='conflicting_statistics':
                quality['fotmob_statistic_conflict_rows']+=1
                if len(conflict_examples)<20:
                    conflict_examples.append({'path':path.relative_to(dataset).as_posix(),'match_id':mid,
                        'player_id':row.get('player_id'),'player_name':row.get('player_name'),
                        'conflicts':json.loads(row.get('fotmob_statistic_group_conflicts') or '[]')})
            key=(row.get('competition') or path.parent.parent.name,row.get('season') or path.parent.name)
            aliases=group_aliases.get((competition_key(key[0]),key[1]),set())
            if len(aliases)==1:key=next(iter(aliases))
            entity=lookup.get((*key,mid))
            if not entity:
                candidates=[v for v in native.get(mid,()) if entity_group.get(v)==key]
                if len(candidates)==1:entity=candidates[0]
            if entity:
                key=entity_group[entity];observed.add(entity);matches[key,table].add(entity)
            elif mid:unlinked[key,table].add(mid)
            if key in groups and table in ('player_match_stats','team_match_stats'):groups[key][table]['rows']+=1
            for field in ('minutes_played','goals','xg','xa','touches'):
                if field in row and row[field] not in ('','NA','NaN','nan','None'):fields[field]+=1
            if row.get('provider'):providers[row['provider']]+=1
        files.append({'path':path.relative_to(dataset).as_posix(),'table':table,'rows':rows,
                      'linked_matches':len(observed),'nonempty_fields':dict(fields),'provider_rows':dict(providers)})
    for key,group in groups.items():
        for table in ('player_match_stats','team_match_stats'):
            group[table]['matches']=len(matches[key,table])
            group[table]['percent']=round(100*len(matches[key,table])/group['fixtures'],2) if group['fixtures'] else 0
        group['unlinked_matches']=sum(len(unlinked[key,t]) for t in TABLES)
    total_fixtures=len(core);player=sum(g['player_match_stats']['matches'] for g in groups.values())
    team=sum(g['team_match_stats']['matches'] for g in groups.values())
    quality['unlinked_match_keys']=sum(len(values) for values in unlinked.values())
    review={'format':FORMAT,'generated_at':datetime.now(timezone.utc).isoformat(),'state':'ready',
            'scope':'Published fixtures and actual packaged local CSV tables; no raw inventory or remote worker claims.',
            'concordance_format':manifest['format'],'concordance_digest':digest(manifest_path(base)),
            'source_files':_inventory(dataset),'summary':{'fixtures':total_fixtures,'player_stat_matches':player,
                'team_stat_matches':team,'player_stat_percent':round(100*player/total_fixtures,2) if total_fixtures else 0,
                'team_stat_percent':round(100*team/total_fixtures,2) if total_fixtures else 0,
                'saved_stats_evidence':sum(g['saved_stats_evidence'] for g in groups.values()),
                'player_stat_rows':sum(f['rows'] for f in files if f['table']=='player_match_stats'),
                'team_stat_rows':sum(f['rows'] for f in files if f['table']=='team_match_stats')},
            'deferred_seasons':list(deferred_seasons),
            'package_diagnostic':package_diagnostic,
            'identity_resolution':json.loads((dataset/'id-resolution.json').read_text())['summary'] if (dataset/'id-resolution.json').is_file() else None,
            'identities':ids,'seasons':[groups[k] for k in sorted(groups)],'files':files,
            'quality':{**quality,'fotmob_statistic_conflict_examples':conflict_examples,
                       'team_namespace_repairs':manifest.get('team_namespace_repairs',0),
                       'invalid_team_provider_ids':manifest.get('invalid_team_provider_ids',0),
                       'reviewed_team_mapping_conflict_entities':len(mapping_conflicts),
                       'reviewed_team_mapping_conflict_examples':mapping_conflicts[:20],
                       'fixture_team_bridge':manifest.get('fixture_team_bridge',{}),'team_bridge_aliases':bridge_aliases}}
    after=[(p.relative_to(dataset).as_posix(),p.stat().st_size,p.stat().st_mtime_ns) for p in _files(dataset)]
    if before!=after or before_manifest!=digest(manifest_path(base)):
        raise ValueError('Dataset changed during coverage review; previous review retained; retry')
    atomic_json(dataset/'coverage.json',review,indent=2)
    return review


def load_review(dataset):
    from concordance.publication import folder,manifest_path,digest
    dataset=Path(dataset);path=manifest_path(folder(dataset))
    if not path.is_file():return {'state':'missing','summary':{},'seasons':[],'message':'No local dataset yet. Run main.py --package.'}
    manifest=json.loads(path.read_text());review_path=dataset/'coverage.json'
    if not review_path.is_file():return {'state':'unreviewed','summary':{'fixtures':manifest['tables']['fixtures/fixtures']['rows']},'seasons':[],
                                       'message':'This dataset predates the coverage review. Run main.py --package to generate one.'}
    value=json.loads(review_path.read_text())
    if value.get('format')!=FORMAT:raise ValueError('Unknown dataset coverage format')
    if value.get('concordance_digest')!=digest(path) or value.get('source_files')!=_inventory(dataset):
        return {**value,'state':'stale','message':'Local dataset files differ from the last packaging review. Run main.py --package to refresh it.'}
    return value


def print_review(review,report=print):
    total=review['summary']
    report('\nDataset coverage review — local packaged files',flush=True)
    report(f"Fixtures: {total['fixtures']:,}; player-stat matches: {total['player_stat_matches']:,} ({total['player_stat_percent']}%); team-stat matches: {total['team_stat_matches']:,} ({total['team_stat_percent']}%)",flush=True)
    competitions=defaultdict(lambda:[0,0,0])
    for row in review['seasons']:
        counts=competitions[row['competition']];counts[0]+=row['fixtures'];counts[1]+=row['player_match_stats']['matches'];counts[2]+=row['team_match_stats']['matches']
    for name,(fixtures,players,teams) in sorted(competitions.items()):
        report(f'  {name}: {fixtures:,} fixtures | player stats {players:,}/{fixtures:,} | team stats {teams:,}/{fixtures:,}',flush=True)
    for item in review.get('deferred_seasons',[]):
        report('  Deferred '+item['collection']+': missing '+', '.join(item['missing_inputs'])+'; previous exports retained; not certified current.',flush=True)
    if review.get('identity_resolution'):report('  Canonical IDs in exported rows: '+str(review['identity_resolution']),flush=True)
    for kind,row in review['identities'].items():
        report(f"  {kind.title()} identities: {row['total']:,}; all three provider IDs {row['all_three']:,}; "+'; '.join(f'{p} {n:,}' for p,n in row['providers'].items()),flush=True)
    if review['quality'].get('unlinked_match_keys'):report(f"Review needed: {review['quality']['unlinked_match_keys']} packaged match keys could not be linked to catalogue fixtures.",flush=True)
    if review['quality'].get('team_namespace_repairs'):
        report(f"Team namespace repair: {review['quality']['team_namespace_repairs']} invalid duplicate references quarantined; valid fixture identities retained. Evidence is in concordance/metadata/team_ids.",flush=True)
    if review['quality'].get('invalid_team_provider_ids'):
        report(f"Team ID audit: {review['quality']['invalid_team_provider_ids']} nonnumeric provider aliases quarantined; original values retained in metadata.",flush=True)
    if review['quality'].get('reviewed_team_mapping_conflict_entities'):
        report(f"Review needed: {review['quality']['reviewed_team_mapping_conflict_entities']} team identities have observed/reviewed ID disagreements. Disputed links are withheld; details are in coverage.json and concordance/metadata/team_ids.",flush=True)
    if review['quality'].get('team_bridge_aliases'):
        report(f"Team bridge: {review['quality']['team_bridge_aliases']} accepted provider aliases; available in R as ds$concordance$team_bridge.",flush=True)
    bridge=review['quality'].get('fixture_team_bridge',{})
    if bridge.get('recovered_aliases'):
        report(f"Fixture team bridge: recovered {bridge['recovered_aliases']} provider aliases for use at home or away.",flush=True)
    if bridge.get('conflicting_aliases'):
        report(f"Review needed: {bridge['conflicting_aliases']} fixture team aliases had conflicting or withheld evidence; no conflicting mapping was overwritten. See coverage.json.",flush=True)
    if review['quality'].get('fotmob_statistic_conflict_rows'):
        report(f"Review needed: {review['quality']['fotmob_statistic_conflict_rows']} player rows contain conflicting FotMob statistics. Disputed components are blank; details are in the season CSVs and coverage.json.",flush=True)
    report('Coverage saved to coverage.json; open the Dataset page in the local dashboard for season details.',flush=True)


def print_package_diagnostic(diagnostic,report=print,*,published=True):
    """Report the outcome only after installation/publication has finished."""
    completed=diagnostic.get('completed',[]);failed=diagnostic.get('failed',[]);deferred=diagnostic.get('deferred',[])
    counts=Counter(item['state'] for item in completed)
    report('\nPackaging diagnostic — '+('published dataset' if published else 'no new dataset published'),flush=True)
    report(f"Seasons completed: {len(completed)} (built {counts['built']}, reused exports {counts['reused']}, unchanged {counts['unchanged']}); failed: {len(failed)}; deferred: {len(deferred)}.",flush=True)
    for item in [*failed,*deferred]:
        reason=item.get('error') or 'missing '+', '.join(item.get('missing_inputs',[]))
        outcome=('previous export retained; not current' if item.get('retained_previous') else 'no export included for this season') if published else 'previous dataset left in place'
        report('  '+item['collection']+': '+reason+'; '+outcome+'.',flush=True)
    if diagnostic.get('fatal_error'):report('  Publication validation failed: '+diagnostic['fatal_error'],flush=True)
    if failed or deferred:
        report('Next step: fix the listed inputs, then run python main.py --package. Incremental mode reuses completed work.',flush=True)
