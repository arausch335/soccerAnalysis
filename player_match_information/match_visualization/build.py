"""Build offline event explorers from the enriched event CSV; stdlib only."""
import argparse,csv,json,math
from pathlib import Path

BASE=Path(__file__).resolve().parent

def number(s):
    try:
        n=float(s)
        return round(n,2) if math.isfinite(n) else None
    except (ValueError,TypeError):return None

def build(source, raw_dir):
    sides={}
    for f in raw_dir.glob('*.json'):
        x=json.loads(f.read_text());p=x.get('payload',x)
        if not isinstance(p,dict):continue
        c=p.get('matchCentreData',{})
        if c.get('home') and c.get('away'):
            sides[str(p.get('match_id',c.get('matchId')))]=[str(c['home']['teamId']),str(c['away']['teamId'])]
    matches={};players={};types=[];teams={}
    with source.open() as f:
        for r in csv.DictReader(f):
            mid=r['match_id'];pid=str(int(float(r['player_id']))) if r['player_id'] else '';tid=r['team_id']
            if pid:players[pid]=r['player_name']
            if tid:teams[tid]=r['team_name']
            if r['event_type'] not in types:types.append(r['event_type'])
            m=matches.setdefault(mid,{'id':mid,'date':r['date'],'home':r['home_team'],'away':r['away_team'],'sides':sides.get(r['provider_match_id']),'events':[],'carries':[],'_rows':[]})
            m['_rows'].append(r)
    for m in matches.values():
        rows=m.pop('_rows'); lookup={r['event_uid']:r for r in rows}
        assert m['sides'],f"Missing home/away mapping: {m['id']}"
        offsets={};elapsed=0
        for period in sorted({int(r['period']) for r in rows if int(r['period']) in (1,2,3,4)}):
            rr=[r for r in rows if int(r['period'])==period];start=min(number(r['match_second']) for r in rr);end=max(number(r['match_second']) for r in rr)
            offsets[period]=(start,elapsed);elapsed+=end-start+1
        for r in rows:
            period=int(r['period'])
            if period not in offsets:continue
            pid=str(int(float(r['player_id']))) if r['player_id'] else ''
            origin,offset=offsets[period];tm=number(r['match_second'])-origin+offset
            x,y=number(r['x']),number(r['y']);ex,ey=number(r['end_x']),number(r['end_y'])
            if r['is_administrative']=='True':x=y=ex=ey=None
            if x is None or y is None or not(0<=x<=100 and 0<=y<=100):x=y=None
            if ex is None or ey is None or not(0<=ex<=100 and 0<=ey<=100):ex=ey=None
            # Only explicit endpoints. Goal-mouth coordinates are a different projection.
            ev=[tm,period,int(float(r['minute'])),int(float(r['second'] or 0)),r['team_id'],pid,types.index(r['event_type']),0 if r['effective_outcome']=='failure' else 1,x,y,ex,ey,r['is_own_goal']=='True',int(r['event_index'])]
            m['events'].append(ev)
            prev=lookup.get(r['carry_origin_event_uid'])
            if prev and r['inferred_carry_distance_m'] and x is not None:
                cx,cy=number(prev['end_x']),number(prev['end_y'])
                if cx is not None and cy is not None and prev['period']==r['period'] and prev['team_id']==r['team_id']:
                    m['carries'].append([pid,cx,cy,x,y,tm,number(r['inferred_carry_distance_m']),ev[13]])
        m['events'].sort(key=lambda e:(e[1],e[0],e[13]))
        m['duration']=max(e[0] for e in m['events'])
    return {'players':players,'teams':teams,'types':types,'matches':sorted(matches.values(),key=lambda m:(m['date'],m['id']))}

def fragment(data):
    return (BASE/'viewer.html').read_text().replace('/*__DATA__*/',json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c'))

def default_raw_dir():
    root=BASE.parent
    candidates=[root/'data/premier-league/2024-2025/raw/whoscored']
    for candidate in candidates:
        if candidate.is_dir():return candidate
    raise FileNotFoundError('No local WhoScored raw cache found. Pass --raw-dir to select one.')

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,default=BASE.parent/'outputs/event-layer-full/events_enriched.csv');ap.add_argument('--raw-dir',type=Path,default=None);ap.add_argument('--output',type=Path,default=BASE.parent/'outputs/match-visualization');ap.add_argument('--inline',type=Path);a=ap.parse_args(argv)
    if not a.source.is_file():raise FileNotFoundError(f'No enriched events at {a.source}. Run run_event_layer.py first.')
    a.raw_dir=a.raw_dir or default_raw_dir()
    before=a.source.stat()
    data=build(a.source,a.raw_dir)
    after=a.source.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError('The enriched export changed during reading; rerun to use a stable export.')
    a.output.mkdir(parents=True,exist_ok=True)
    # Local standalone viewer uses the same controls and data code as the inline preview.
    kit=(BASE/'viewer.css').read_text()
    full='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Player maps & match replay</title><style>'+kit+'\nbody{max-width:1050px;margin:auto;padding:24px}</style></head><body>'+fragment(data)+'</body></html>'
    temporary=a.output/'match-explorer.html.tmp'
    temporary.write_text(full,encoding='utf-8')
    temporary.replace(a.output/'match-explorer.html')
    if a.inline:
        small={**data,'matches':[m for m in data['matches'] if m['id'] in ['12436920','12436896','12436969']]}
        ids={e[5] for m in small['matches'] for e in m['events']};small['players']={k:v for k,v in data['players'].items() if k in ids}
        a.inline.parent.mkdir(parents=True,exist_ok=True);a.inline.write_text(fragment(small));assert a.inline.stat().st_size<1_000_000
    print(json.dumps({'matches':len(data['matches']),'events':sum(len(m['events']) for m in data['matches']),'players':len(data['players']),'output':str(a.output/'match-explorer.html')}))

    manifest={'source':str(a.source.resolve()),'raw_dir':str(a.raw_dir.resolve()),
              'source_bytes':before.st_size,'source_modified_ns':before.st_mtime_ns,
              'matches':len(data['matches']),'events':sum(len(m['events']) for m in data['matches']),
              'players':len(data['players']),'output':str((a.output/'match-explorer.html').resolve())}
    (a.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return a.output/'match-explorer.html'

if __name__=='__main__':
    main()
