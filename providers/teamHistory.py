"""Analysis-only team history edges, separate from same-team ID mappings."""
from datetime import date
import hashlib
import json
from pathlib import Path

from .teamScope import in_mens_scope

PROVIDERS=('sofascore','fotmob','whoscored')
RELATIONSHIPS={'renamed_to','succeeded_by','merged_into','split_into','transferred_rights_to','distinct_from'}
CATALOG=Path(__file__).parent/'concordance/team_history.json'
BASE_COLUMNS=['relationship_id','event_id','relationship','from_team_key','from_name','to_team_key','to_name',
              'effective_date','effective_year','date_precision','status','gender','used_for_identity_matching','source_urls','notes']
ENDPOINT_COLUMNS=[side+'_'+field for side in ('from','to') for field in
                  (*[p+'_id' for p in PROVIDERS],'reep_id','reep_ids')]
COLUMNS=BASE_COLUMNS+ENDPOINT_COLUMNS


def history_frame(root=None,*,catalog=None):
    """Return the extra team concordance dataframe; never modify fixtures."""
    import pandas as pd
    from .reep import index
    value=json.loads(Path(catalog or CATALOG).read_text(encoding='utf-8'))
    if value.get('format')!='soccer-team-history-v1':raise ValueError('Invalid team history catalog')
    teams=value['teams'];bridges=index(root);records=[];seen=set()
    for edge in value['relationships']:
        if edge['relationship'] not in RELATIONSHIPS:raise ValueError('Unknown team history relationship')
        if (edge['from']==edge['to'])!=(edge['relationship']=='renamed_to'):
            raise ValueError('Only a rename can reuse the same team era key')
        if edge['id'] in seen:raise ValueError('Duplicate team history relationship ID')
        seen.add(edge['id'])
        if edge['status'] not in ('researched','confirmed'):raise ValueError('Invalid team history review status')
        exact=edge.get('effective_date','');year=edge.get('effective_year','')
        if exact and (date.fromisoformat(exact).year!=year):raise ValueError('Team history date/year mismatch')
        precision='day' if exact else 'year' if year else 'unknown'
        row=dict(relationship_id=edge['id'],event_id=edge['event_id'],relationship=edge['relationship'],
                 from_team_key=edge['from'],to_team_key=edge['to'],effective_date=exact,effective_year=year,
                 date_precision=precision,status=edge['status'],gender='male',used_for_identity_matching=False,
                 source_urls=json.dumps(edge['sources'],ensure_ascii=False),notes=edge.get('notes',''))
        if not edge['sources'] or any(not u.startswith('https://') for u in edge['sources']):raise ValueError('Missing team history provenance')
        for side in ('from','to'):
            team=teams[edge[side]]
            if team.get('gender')!='male':raise ValueError('Team history must stay in the men\'s scope')
            row[side+'_name']=edge.get(side+'_name',team['name'])
            reep_ids=set()
            for provider in PROVIDERS:
                native=team.get(provider+'_id','')
                if native and (not native.isdigit() or not in_mens_scope(provider,native)):
                    raise ValueError('Out-of-scope team history profile')
                row[side+'_'+provider+'_id']=native
                # Keep upstream disagreements visible; never collapse historical
                # teams or chase succession as though it were an identity alias.
                reep_ids.update(bridges.canonical_id(r) for r in bridges.native.get(('team',provider,native),set()))
            row[side+'_reep_ids']=json.dumps(sorted(reep_ids))
            row[side+'_reep_id']=next(iter(reep_ids)) if len(reep_ids)==1 else ''
        records.append(row)
    return pd.DataFrame(records,columns=COLUMNS)


def export_history(root,output):
    from collection.files import atomic_csv
    frame=history_frame(root);path=Path(output)/'concordance/team_history.csv'
    atomic_csv(frame,path)
    return path,{'rows':len(frame),'format':'soccer-team-history-v1','scope':'men',
                 'catalog_sha256':hashlib.sha256(CATALOG.read_bytes()).hexdigest()}
