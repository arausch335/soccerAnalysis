"""Repair legacy exported period-only scores from saved provider evidence."""
from datetime import datetime,timezone
import json
from pathlib import Path

from providers.scores import final_score


def repair_exported_scores(rows,root,bridges):
    root=Path(root);events={};repairs=[]
    # These are small saved schedules/identity evidence, never full raw matches.
    for path in root.glob('*/*/schedule.json'):
        if path.parent.parent.name=='_system':continue
        cfg=path.with_name('config.json')
        if not cfg.exists():continue
        config=json.loads(cfg.read_text(encoding='utf-8'))
        if config.get('id_namespace',config.get('baseline_provider','sofascore'))!='sofascore':continue
        for event in json.loads(path.read_text(encoding='utf-8')).get('events',[]):events[str(event['id'])]=event
    evidence=root/'_system/identity/fixture_score_evidence.json'
    if evidence.exists():
        saved=json.loads(evidence.read_text(encoding='utf-8'))
        if saved.get('format')!='soccer-fixture-score-evidence-v1':raise ValueError('Invalid fixture score evidence')
        for event in saved.get('events',[]):events[str(event['id'])]=event
    output=[];cache={}
    for original in rows:
        row=dict(original);event=events.get(row.get('sofascore_match_id'))
        if (not event or row.get('status')!='finished' or event.get('status',{}).get('type')!='finished'
                or datetime.fromtimestamp(event['startTimestamp'],timezone.utc).date().isoformat()!=row.get('date')
                or any(str(event.get(s+'Team',{}).get('id'))!=row.get('sofascore_'+s+'_team_id') for s in ('home','away'))):
            output.append(row);continue
        scores=[final_score(event,s) for s in ('home','away')]
        if any(v is None for v in scores):output.append(row);continue
        if [row.get(s+'_score') for s in ('home','away')]!=list(map(str,scores)):
            repairs.append(dict(sofascore_match_id=row['sofascore_match_id'],date=row['date'],
                home_team=row['home_team'],away_team=row['away_team'],
                old_home_score=row.get('home_score'),old_away_score=row.get('away_score'),
                home_score=scores[0],away_score=scores[1],source='sofascore_saved_period_scores'))
            row['home_score'],row['away_score']=map(str,scores)
            row['winner_code']=str(event.get('winnerCode') or (1 if scores[0]>scores[1] else 2 if scores[0]<scores[1] else 3))
        resolved=bridges.resolve('match',{p+'_id':row.get(p+'_match_id','') for p in ('sofascore','fotmob','whoscored')})
        revalidated=False
        for provider in ('fotmob','whoscored'):
            if row.get(provider+'_fixture_check')!='reep_fixture_failed_validation':continue
            native=resolved.get(provider+'_id')
            if not native or resolved['identity_status']=='conflict':continue
            competition=row['competition'].removesuffix(' Qualification')
            slug=competition.lower().replace(' ','-').replace('.','')
            key=(provider,slug,row['season'])
            if key not in cache:
                folder=root/'_system/fixtures'
                paths=[folder/provider/slug/(row['season']+'.json'),folder/'cross_provider'/provider/slug/(row['season']+'.json')]
                cache[key]={str(e['id']):e for path in paths if path.exists()
                            for e in json.loads(path.read_text(encoding='utf-8')).get('events',[])}
            candidate=cache[key].get(native)
            if not candidate:continue
            from .dataset import _compatible
            if _compatible(event,candidate,provider,root,bridges):
                row[provider+'_match_id']=native;row[provider+'_fixture_check']='validated'
                row[provider+'_match_method']='saved_period_scores_and_fixture';revalidated=True
        checks=[row.get(p+'_fixture_check','missing') for p in ('fotmob','whoscored')]
        if revalidated and all(c in ('validated','bridge_only','missing','') for c in checks):
            row['match_identity_status']='unmapped'
        output.append(row)
    return output,repairs
