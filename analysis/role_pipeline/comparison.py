"""Within-position distances and explicitly exploratory cluster diagnostics."""
from collections import Counter
import json
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans,AgglomerativeClustering
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score,adjusted_rand_score,adjusted_mutual_info_score
from sklearn.metrics import pairwise_distances
from threadpoolctl import threadpool_limits


def transform(values,specs):
    """Fit cohort-only transforms. Each conceptual block gets equal distance weight."""
    x=np.array(values,dtype=float,copy=True);params=[]
    for j,s in enumerate(specs):
        col=x[:,j];finite=col[np.isfinite(col)]
        if not len(finite):raise ValueError('Entirely missing model feature')
        median=float(np.median(finite));col=np.where(np.isfinite(col),col,median)
        if s['group']=='field regions':col=np.sqrt(np.clip(col,0,1));kind='sqrt composition'
        elif s['kind']=='count':col=np.log1p(np.maximum(col,0));kind='log1p'
        else:kind='identity'
        lo,hi=np.quantile(col,[.01,.99]);col=np.clip(col,lo,hi)
        center=np.median(col);scale=np.quantile(col,.75)-np.quantile(col,.25)
        if scale<1e-9:scale=np.std(col)
        if scale<1e-9:scale=1.
        x[:,j]=np.clip((col-center)/scale,-4,4)
        params.append({'feature':s['feature'],'impute_median':median,'transform':kind,'clip_low':lo,'clip_high':hi,'center':center,'scale':scale})
    for group in sorted({s['group'] for s in specs}):
        cols=[i for i,s in enumerate(specs) if s['group']==group]
        scale=np.sqrt(np.var(x[:,cols],axis=0).sum())
        if scale>0:x[:,cols]/=scale
        for i in cols:params[i]['block_scale']=float(scale);params[i]['block']=group
    return x,params

def labels_for(x,method,k,seed):
    if method=='kmeans':return KMeans(n_clusters=k,n_init=30,random_state=seed).fit_predict(x)
    return AgglomerativeClustering(n_clusters=k,linkage='ward').fit_predict(x)


def compare(profiles,coverage,registry,contrib,config):
    profiles=profiles.copy();profiles['role_cluster']=None;profiles['cluster_method']=None;profiles['cluster_status']=None
    profiles['model_features_observed']=np.nan;profiles['model_eligible']=False
    profiles['pca_x']=np.nan;profiles['pca_y']=np.nan
    specs_by={s['feature']:s for s in registry};all_features=[s['feature'] for s in registry]
    cov=coverage.pivot(index='profile_id',columns='feature',values='minute_coverage')
    pct=[];neighbors=[];evaluations=[];selections=[];matrices=[];cluster_profiles=[];models={};uncertainty=[]
    rng=np.random.default_rng(config.seed)
    eligible=profiles[profiles.eligible_comparison]
    with threadpool_limits(limits=1):
      for key,group in eligible.groupby(['competition','season','position_group'],sort=True):
        cohort='|'.join(key);ids=group.profile_id.tolist();n=len(group)
        valid=group.set_index('profile_id')
        coverage_here=cov.reindex(ids)
        for s in registry:
            f=s['feature'];v=valid[f].where(coverage_here[f]>=config.min_feature_coverage)
            nn=int(v.notna().sum())
            if nn>=8:
                ranks=(v.rank(method='average')-.5)/nn*100
                pct.extend({'profile_id':pid,'feature':f,'percentile':rank,'peers_observed':nn,'cohort':cohort} for pid,rank in ranks.dropna().items())
        if n<config.min_cohort:
            profiles.loc[group.index,'cluster_status']='insufficient peer sample';continue
        selected=[]
        for s in registry:
            if not s['candidate']:continue
            f=s['feature'];v=valid[f].where(coverage_here[f]>=config.min_feature_coverage)
            reason=''
            if v.notna().mean()<config.min_feature_coverage:reason='insufficient metric coverage'
            elif v.dropna().nunique()<4:reason='too little variation'
            elif s['kind']=='count' and v.gt(0).mean()<.1:reason='rare activity'
            elif s['group']=='goalkeeping' and key[2]!='Goalkeeper':reason='goalkeeper-only feature'
            if not reason:
                for prev in selected:
                    if specs_by[prev]['group']!=s['group']:continue
                    corr=v.corr(valid[prev],method='spearman')
                    if np.isfinite(corr) and abs(corr)>.90:
                        reason='redundant with '+prev;break
            if not reason:selected.append(f)
            selections.append({'cohort':cohort,'feature':f,'selected':not reason,'reason':reason or 'selected','block':s['group']})
        if len(selected)<5:
            profiles.loc[group.index,'cluster_status']='insufficient supported features';continue
        values=valid[selected].where(coverage_here[selected]>=config.min_feature_coverage)
        observed=values.notna().mean(axis=1)
        keep=observed>=config.min_feature_coverage
        profiles.loc[group.index,'model_features_observed']=observed.to_numpy()
        profiles.loc[group.index[~keep.to_numpy()],'cluster_status']='too many missing role features'
        values=values.loc[keep];valid=valid.loc[keep];ids=values.index.tolist();group=group[group.profile_id.isin(ids)];n=len(ids)
        if n<config.min_cohort:
            profiles.loc[group.index,'cluster_status']='insufficient peers after coverage filter';continue
        ss=[specs_by[f] for f in selected];x,params=transform(values.to_numpy(),ss)
        pca=PCA(n_components=2,random_state=config.seed);projection=pca.fit_transform(x)
        idx=profiles.set_index('profile_id').loc[ids].index
        for i,pid in enumerate(ids):
            mask=profiles.profile_id.eq(pid);profiles.loc[mask,['pca_x','pca_y']]=projection[i];profiles.loc[mask,'model_eligible']=True
        for i,pid in enumerate(ids):
            matrices.extend({'cohort':cohort,'profile_id':pid,'feature':f,'raw_value':values.iloc[i,j],
                 'model_value':x[i,j],'imputed':pd.isna(values.iloc[i,j]),'block':ss[j]['group']} for j,f in enumerate(selected))
        distances=pairwise_distances(x)
        for i,pid in enumerate(ids):
            for rank,j in enumerate([j for j in np.argsort(distances[i]) if j!=i][:10],1):
                neighbors.append({'profile_id':pid,'peer_profile_id':ids[j],'rank':rank,'distance':distances[i,j],'cohort':cohort})
        candidates=[]
        min_size=max(5,int(np.ceil(n*.05)))
        for method in ['kmeans','ward']:
            for k in range(2,min(6,n//min_size)+1):
                lab=labels_for(x,method,k,config.seed);sizes=Counter(lab);score=silhouette_score(x,lab)
                admissible=min(sizes.values())>=min_size
                row={'cohort':cohort,'method':method,'k':k,'players':n,'features':len(selected),'silhouette':score,
                     'smallest_cluster':min(sizes.values()),'cluster_sizes':json.dumps(dict(sorted((int(a),b) for a,b in sizes.items()))),
                     'admissible':admissible,'selected':False,'bootstrap_ari_mean':np.nan,'bootstrap_ari_p10':np.nan}
                evaluations.append(row)
                if admissible:candidates.append((score,method,k,lab,row))
        models[cohort]={'features':selected,'transforms':params,'pca_explained_variance':pca.explained_variance_ratio_.tolist(),'pca_components':pca.components_.tolist(),'pca_mean':pca.mean_.tolist(),'members':ids}
        if not candidates:
            profiles.loc[group.index,'cluster_status']='no partition meets minimum group size';continue
        best=max(candidates,key=lambda z:z[0]);score,method,k,lab,record=best;record['selected']=True
        # Resample each player's matches, not just the random clustering seed.
        col_indices=[all_features.index(f) for f in selected]
        samples=[]
        for _ in range(config.bootstrap_runs):
            vals=[]
            for pid in ids:
                c=contrib[pid];draw=rng.integers(0,len(c['indices']),len(c['indices']))
                nn=c['num'][draw][:,col_indices].sum(axis=0);dd=c['den'][draw][:,col_indices].sum(axis=0)
                vals.append(np.divide(nn,dd,out=np.full(len(selected),np.nan),where=dd>0))
            vals=np.array(vals)
            # Rare bootstrap-all-missing columns use observed cohort medians, explicitly.
            for j in range(vals.shape[1]):
                if not np.isfinite(vals[:,j]).any():vals[:,j]=np.nanmedian(values.iloc[:,j])
            xb,_=transform(vals,ss);samples.append(adjusted_rand_score(lab,labels_for(xb,method,k,config.seed)))
        record['bootstrap_ari_mean']=float(np.mean(samples)) if samples else np.nan
        record['bootstrap_ari_p10']=float(np.quantile(samples,.1)) if samples else np.nan
        record['team_adjusted_mutual_information']=float(adjusted_mutual_info_score(valid.teams,lab))
        status='exploratory: weak separation' if score<.15 else 'exploratory: unstable' if not samples or np.mean(samples)<.6 else 'exploratory: repeatable partition'
        # Neutral labels; derive descriptions from measured contrasts instead of naming roles in advance.
        for j,pid in enumerate(ids):
            mask=profiles.profile_id.eq(pid);profiles.loc[mask,'role_cluster']=int(lab[j])+1;profiles.loc[mask,'cluster_method']=method;profiles.loc[mask,'cluster_status']=status
        z=(values-values.mean())/values.std().replace(0,np.nan)
        for cluster in sorted(set(lab)):
            members=np.where(lab==cluster)[0];contrast=z.iloc[members].mean().sort_values()
            desc='Higher '+', '.join(specs_by[f]['label'] for f in contrast.tail(3).index[::-1])+'; lower '+', '.join(specs_by[f]['label'] for f in contrast.head(2).index)
            cluster_profiles.append({'cohort':cohort,'cluster':int(cluster)+1,'method':method,'members':len(members),
               'description':desc,'status':status,'silhouette':score,'bootstrap_ari_mean':record['bootstrap_ari_mean']})
        models[cohort].update({'selected_method':method,'k':k,'status':status,'silhouette':score,
            'bootstrap_ari_mean':record['bootstrap_ari_mean'],'bootstrap_ari_p10':record['bootstrap_ari_p10']})
        # Match-resampled intervals on raw selected metrics preserve exposure denominators.
        for pid in ids:
            c=contrib[pid];draw=rng.integers(0,len(c['indices']),(200,len(c['indices'])))
            nn=c['num'][draw][:,:,col_indices].sum(axis=1);dd=c['den'][draw][:,:,col_indices].sum(axis=1)
            rates=np.divide(nn,dd,out=np.full(nn.shape,np.nan),where=dd>0)
            for j,f in enumerate(selected):
                arr=rates[:,j];arr=arr[np.isfinite(arr)]
                uncertainty.append({'profile_id':pid,'feature':f,'bootstrap_p10':np.quantile(arr,.1) if len(arr) else np.nan,
                    'bootstrap_p90':np.quantile(arr,.9) if len(arr) else np.nan,'resamples':len(arr)})
    return profiles,{
      'comparison_percentiles':pd.DataFrame(pct,columns=['profile_id','feature','percentile','peers_observed','cohort']),'nearest_players':pd.DataFrame(neighbors,columns=['profile_id','peer_profile_id','rank','distance','cohort']),
      'cluster_diagnostics':pd.DataFrame(evaluations,columns=['cohort','method','k','players','features','silhouette','smallest_cluster','cluster_sizes','admissible','selected','bootstrap_ari_mean','bootstrap_ari_p10','team_adjusted_mutual_information']),'model_feature_selection':pd.DataFrame(selections),
      'role_model_matrix':pd.DataFrame(matrices,columns=['cohort','profile_id','feature','raw_value','model_value','imputed','block']),'cluster_profiles':pd.DataFrame(cluster_profiles),
      'season_metric_uncertainty':pd.DataFrame(uncertainty,columns=['profile_id','feature','bootstrap_p10','bootstrap_p90','resamples'])},models
