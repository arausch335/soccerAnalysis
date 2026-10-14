"""Check exported role-analysis invariants without rereading source data."""
import json
from pathlib import Path
import numpy as np
import pandas as pd


def validate(output):
    p=Path(output);season=pd.read_csv(p/'player_season_roles.csv');pos=pd.read_csv(p/'player_position_roles.csv');match=pd.read_csv(p/'player_match_roles.csv',low_memory=False)
    assert not season.duplicated(['competition','season','player_id']).any()
    assert not pos.duplicated(['competition','season','player_id','position_group']).any()
    assert not match.duplicated(['match_id','player_id']).any()
    assert np.isclose(season.minutes.sum(),match.minutes_played.sum())
    assert np.isclose(pos.minutes.sum(),match.minutes_played.sum())
    for family in ['on_ball','pass_origin','pass_destination','take_on','shot','defensive','recovery','reception_inferred']:
        cols=[c for c in season if c.startswith('zone_'+family+'_') and c.endswith('_share')]
        sums=season[cols].sum(axis=1,min_count=1).dropna();assert np.allclose(sums,1),family
    neighbors=pd.read_csv(p/'nearest_players.csv');mapping=pos.set_index('profile_id')
    for row in neighbors.itertuples():
        a=mapping.loc[row.profile_id];b=mapping.loc[row.peer_profile_id]
        assert (a.competition,a.season,a.position_group)==(b.competition,b.season,b.position_group)
        assert a.player_id!=b.player_id
    matrix=pd.read_csv(p/'role_model_matrix.csv');catalog=pd.read_csv(p/'metric_catalog.csv').set_index('feature')
    assert all(catalog.loc[f,'scope']=='role' and bool(catalog.loc[f,'candidate']) for f in matrix.feature.unique())
    assert np.isfinite(matrix.model_value).all()
    coverage=pd.read_csv(p/'season_metric_coverage.csv');assert coverage.minute_coverage.between(0,1+1e-9).all()
    intervals=pd.read_csv(p/'season_metric_uncertainty.csv')
    assert ((intervals.bootstrap_p10<=intervals.bootstrap_p90) | (intervals.bootstrap_p10.isna()&intervals.bootstrap_p90.isna()&intervals.resamples.eq(0))).all()
    for pid in season.nlargest(4,'minutes').player_id:
        g=match[match.player_id.eq(pid)];s=season[season.player_id.eq(pid)].iloc[0];known=g.passes_attempted.notna()
        expected=g.loc[known,'passes_attempted'].sum()*90/g.loc[known,'minutes_played'].sum()
        assert np.isclose(s.passes_attempted_per90,expected)
    result={'status':'passed','checks':['unique profile keys','minute conservation','region probability sums',
       'within-position neighbors','role-only model inputs','finite model matrix','coverage bounds',
       'bootstrap interval bounds and missingness','exposure-weighted rates'],
       'season_profiles':len(season),'position_profiles':len(pos),'modeled_profiles':int(pos.model_eligible.sum()),'neighbor_pairs':len(neighbors)}
    (p/'validation.json').write_text(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    import sys
    print(json.dumps(validate(sys.argv[1]),indent=2))
