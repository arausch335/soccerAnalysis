"""Portable local analysis report; no remote resources or API calls."""
import json
from pathlib import Path
import pandas as pd
from .config import ZONES,FAMILIES

def records(df):return json.loads(df.to_json(orient='records'))

def write_report(output,summary,profiles,registry,comparisons,models,metric_audit,matches):
    keep=['profile_id','competition','season','player_id','player_name','position_group','dominant_season_position','teams','matches','minutes','event_minutes','event_minute_coverage','first_match','last_match','position_minutes_json','eligible_comparison','eligibility_reason','model_eligible','role_cluster','cluster_method','cluster_status','pca_x','pca_y','spatial_match_variation_jsd']
    keep+=[s['feature'] for s in registry]
    keep+=[c for c in profiles if c.startswith('zone_')]
    match_keep=['match_id','date','season','team','opponent','player_id','player_name','minutes_played','detailed_position','dominant_position_share','comparison_position','event_covered']
    match_keep += [s['feature'] for s in registry if s['candidate']]
    descriptions={};spec_by={s['feature']:s for s in registry}
    percentile_rows=comparisons['comparison_percentiles']
    for row in profiles.to_dict('records'):
        model=models.get(row['competition']+'|'+row['season']+'|'+row['position_group'])
        if not model or not row['model_eligible']:continue
        ranks=percentile_rows[percentile_rows.profile_id.eq(row['profile_id']) & percentile_rows.feature.isin(model['features'])].copy()
        ranks['contrast']=(ranks.percentile-50).abs();chosen=[];seen=set()
        for r in ranks.sort_values('contrast',ascending=False).to_dict('records'):
            spec=spec_by[r['feature']]
            if spec['group'] in seen:continue
            seen.add(spec['group']);chosen.append(f"{spec['label']} (percentile {r['percentile']:.0f})")
            if len(chosen)==3:break
        descriptions[row['profile_id']]='Distinctive activity within this position: '+ '; '.join(chosen)+'.'
    narratives=[]
    for row in profiles[profiles.position_group.eq('All positions')].to_dict('records'):
        positions=profiles[profiles.player_id.eq(row['player_id']) & profiles.season.eq(row['season']) & profiles.position_group.ne('All positions')]
        eligible_positions=positions[positions.model_eligible].sort_values('minutes',ascending=False)
        reference=eligible_positions.iloc[0] if len(eligible_positions) else None
        descriptions[row['profile_id']]=(f"Primary observed usage: {row['dominant_season_position']}. "+
            (f"In {reference['position_group']} matches: "+descriptions.get(reference['profile_id'],'') if reference is not None else 'Insufficient comparable match exposure for a within-position style description.'))
        narratives.append({'player_id':row['player_id'],'player_name':row['player_name'],'competition':row['competition'],
           'season':row['season'],'observed_matches':row['matches'],'observed_minutes':row['minutes'],
           'event_minute_coverage':row['event_minute_coverage'],'position_usage':json.loads(row['position_minutes_json']),
           'description':descriptions[row['profile_id']],
           'position_profiles':records(positions[['profile_id','position_group','minutes','matches','eligible_comparison','role_cluster','cluster_status']])})
    (output/'player_role_profiles.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in narratives)+'\n',encoding='utf-8')
    payload={'summary':summary,'descriptions':descriptions,'profiles':records(profiles[keep]),'registry':registry,
       'percentiles':records(comparisons['comparison_percentiles']),
       'neighbors':records(comparisons['nearest_players']),
       'diagnostics':records(comparisons['cluster_diagnostics']),
       'clusters':records(comparisons['cluster_profiles']),
       'models':models,'matches':records(matches[match_keep]),'zones':ZONES,'families':FAMILIES,
       'uncertainty':records(comparisons['season_metric_uncertainty']),
       'catalog':metric_audit}
    # Round model display values; CSV outputs retain full precision.
    serialized=json.dumps(payload,separators=(',',':'),default=lambda x:x.item()).replace('<','\\u003c')
    from plotly.offline import get_plotlyjs
    html=(Path(__file__).parent/'explorer.html').read_text().replace('/*__DATA__*/',serialized).replace('/*__PLOTLY__*/',get_plotlyjs().replace('</script','<\\/script'))
    (output/'role-explorer.html').write_text(html,encoding='utf-8')
    eligible=profiles[profiles.eligible_comparison]
    selected=comparisons['cluster_diagnostics']
    if not selected.empty:selected=selected[selected.selected]
    unavailable=[x['name'] for x in metric_audit if x['scope']=='role' and x['availability']=='unavailable in current export']
    lines=['# Competition role analysis','',
       f"{summary['competition']} {', '.join(summary['seasons'])}: {summary['observed_matches']} of {summary['scheduled_matches'] or 'unknown'} scheduled matches, from {summary['observed_date_from']} to {summary['observed_date_to']}.",
       'This describes the observed season sample, not a completed-season census.' if not summary['season_complete'] else 'The collected schedule is fully represented.','',
       f"{summary['unique_players']} players; {summary['appearances']} positive-minute appearances; {summary['event_covered_appearances']} have usable event streams. {summary['eligible_comparison_profiles']} player-position profiles meet the comparison thresholds; {summary['modeled_profiles']} also meet model coverage and peer-count requirements.",'',
       '## Cluster evidence','',
       '| Position | Players | Method | Groups | Silhouette | Mean bootstrap ARI | Interpretation |',
       '|---|---:|---|---:|---:|---:|---|']
    for r in selected.to_dict('records'):
        mod=models[r['cohort']];lines.append(f"| {r['cohort'].split('|')[-1]} | {r['players']} | {r['method']} | {r['k']} | {r['silhouette']:.3f} | {r['bootstrap_ari_mean']:.3f} | {mod['status']} |")
    lines += ['', 'Cluster labels are provisional descriptions of activity and geography. They are not tactical ground truth or player quality ratings. Choosing a partition does not establish that discrete football roles exist.', '',
       '## Coverage and interpretation','',
       '- Statistics and event data join through the existing match-specific provider identity bridge; names are never used as new join keys.',
       '- Positions come from source formation slots. Match statistics are attributed to the dominant position for that match; they are not fabricated minute-by-minute position statistics. Mixed matches remain in the all-position season profile.',
       '- Per-90 values use only minutes from matches where that metric is observed. Ratios sum compatible numerators and denominators before division. Every value has an observed-minutes record.',
       '- The spatial model uses folded open-play on-ball regions; full left/right 15-zone distributions are also exported for all action families. Touch maps and inferred receptions/carries remain separate evidence.',
       '- Role distances exclude goals, assists, ratings, completion rates, adverse outcomes, and unverified physical quantities. A high percentile means more of an activity, not better performance.',
       '- Team style and opposition affect role observations. Team association is reported for clusters; no claim of tactical causality or possession-adjusted ability is made.',
       '- Resampling intervals and cluster stability are conditional on these observed matches, not population-level confidence guarantees.',
       '', 'Unavailable role metrics: '+(', '.join(unavailable) if unavailable else 'None')+'.','',
       '## Local outputs','',
       'Open `role-explorer.html` for player profiles, same-position comparisons, regions, match history, and cluster diagnostics. The CSVs retain the full analysis tables and coverage denominators. `models.json` records selected features, fitted transforms, seeds/configuration (in the manifest), and PCA projection variance.', '',
       '## Method references','',
       '- [scikit-learn clustering documentation](https://scikit-learn.org/stable/modules/clustering.html): K-means, Ward hierarchical clustering, silhouette evaluation, and adjusted Rand index.',
       '- [Robust scaling principles](https://scikit-learn.org/stable/modules/generated/sklearn.preprocessing.RobustScaler.html): median and interquartile-range scaling; this pipeline additionally documents clipping and equal block weighting.',
       '- Local role/performance contract: `features.py`. Event geometry and inference definitions: `analysis/event_layer/pipeline.py`.']
    (output/'analysis.md').write_text('\n'.join(lines),encoding='utf-8')
