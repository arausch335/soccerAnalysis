"""Saved-stat availability and explicitly planned scraping on one fixture table."""
from pathlib import Path
import csv,json

from providers.reep import PROVIDERS

FLAGS=('has_player_stats','is_scheduled_for_scraping')


def truth(value):return str(value).lower()=='true'


def native_matches(row):
    generic=any(p+'_id' in row for p in PROVIDERS)
    values=[]
    for p in PROVIDERS:
        native=row.get(p+'_id') if generic else row.get(p+'_match_id')
        if not native:continue
        if not generic and p!='sofascore' and row.get(p+'_fixture_check') not in (None,'','validated','bridge_only'):continue
        values.append((p,str(native)))
    safe=row.get('safe_for_cross_provider_joins')
    return values[:1] if safe is not None and not truth(safe) else values


def scrape_targets():
    from config.workerPlan import load_plan
    plan=load_plan()
    return {(j.competition,j.as_dict()['season']) for worker in plan.workers for j in plan.for_worker(worker)}


def stats_evidence(root,dataset):
    """Use saved validated inventories; an ID bridge or event stream isn't stats."""
    from collection.reporting import inventory_paths
    root,dataset=Path(root),Path(dataset);observed=set()
    for path in inventory_paths(root.parent/'ensemble_data'):
        value=json.loads(path.read_text(encoding='utf-8'))
        for row in value.get('records',{}).values():
            ns=row.get('namespace');resources=row.get('resources',{});caps=row.get('capabilities',[])
            if ns=='sofascore' and (resources.get('sofascore_stats')=='complete' or 'sofascore_match_stats' in caps):
                observed.add(('sofascore',str(row['match_id'])))
            elif ns=='fotmob_players' and resources.get('fotmob_stats')=='complete':
                observed.add(('fotmob',str(row['match_id'])))
    # A previously validated integrated snapshot also supplies compact evidence.
    path=dataset/'concordance/full/fixtures.csv'
    if path.is_file():
        with path.open(encoding='utf-8-sig',newline='') as stream:
            for row in csv.DictReader(stream):
                if truth(row.get('has_player_stats')):observed.update(native_matches(row))
    return observed


def annotate(rows,*,observed,targets):
    for row in rows:
        has=any(key in observed for key in native_matches(row))
        competition=row.get('competition','').removesuffix(' Qualification')
        if row.get('competition','').endswith(' Qualification'):competition+=' Qualifiers'
        target=(competition,str(row.get('season','')).replace('/','-')) in targets
        row.update(has_player_stats=has,is_scheduled_for_scraping=bool(target and not has
            and row.get('status') not in ('canceled','cancelled','abandoned')))
    return rows
