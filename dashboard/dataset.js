'use strict';
let datasetReview=null;
async function loadDataset(){
  const response=await fetch('/api/dataset');
  if(!response.ok)throw new Error('The local dataset review is temporarily unavailable.');
  datasetReview=await response.json();
  options('dataset-competition',[...new Set((datasetReview.seasons||[]).map(r=>r.competition))].sort().map(v=>[v,v]));
  options('dataset-season',[...new Set((datasetReview.seasons||[]).map(r=>r.season))].sort().reverse().map(v=>[v,v]));
  renderDataset();
}
function datasetRatio(matches,total){
  const percent=total?Math.min(100,100*matches/total):0;
  return `<strong>${fmt(matches)} / ${fmt(total)}</strong><div class="dataset-progress"><span data-width="${percent}"></span></div><small>${percent.toFixed(1)}% of catalogue fixtures</small>`;
}
function renderDataset(){
  if(!datasetReview)return;
  const value=datasetReview,s=value.summary||{},available=value.state==='ready'||value.state==='stale';
  $('dataset-note').textContent=value.message||(value.generated_at?'Packaging review · '+when(value.generated_at)+' · '+(value.concordance_format.includes('parquet')?'Parquet':'CSV'):'No completed package review');
  if(value.quality?.unlinked_match_keys)$('dataset-note').textContent+=' · '+fmt(value.quality.unlinked_match_keys)+' packaged match keys need linkage review';
  $('dataset-note').classList.toggle('dataset-stale',value.state!=='ready');
  $('dataset-summary').innerHTML=[['Catalogue fixtures',s.fixtures,'Published fixtures in this local dataset'],
    ['Player-stat matches',available?s.player_stat_matches:null,available?s.player_stat_percent+'% · '+fmt(s.player_stat_rows)+' player rows':'Package the dataset to measure coverage'],
    ['Team-stat matches',available?s.team_stat_matches:null,available?s.team_stat_percent+'% · '+fmt(s.team_stat_rows)+' statistic rows':'Package the dataset to measure coverage'],
    ['Saved-stat evidence',available?s.saved_stats_evidence:null,'Fixture flags; separate from packaged table coverage']]
    .map(([label,count,note])=>`<article><small>${esc(label)}</small><strong>${count==null?'—':fmt(count)}</strong><span>${esc(note)}</span></article>`).join('');
  const competition=$('dataset-competition').value,season=$('dataset-season').value;
  const rows=(value.seasons||[]).filter(r=>(!competition||r.competition===competition)&&(!season||r.season===season));
  $('dataset-season-rows').innerHTML=rows.map(r=>`<tr><td>${esc(r.competition)}</td><td>${esc(r.season)}</td><td>${fmt(r.fixtures)}</td><td>${datasetRatio(r.player_match_stats.matches,r.fixtures)}</td><td>${datasetRatio(r.team_match_stats.matches,r.fixtures)}</td><td>${r.unlinked_matches?fmt(r.unlinked_matches)+' need review':'—'}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">No packaged season review for this selection.</td></tr>';
  const identities=value.identity_resolution;
  $('dataset-identity-resolution').textContent=identities?`Across all packaged tables: ${fmt(identities.status_counts.resolved||0)} / ${fmt(identities.rows)} rows have all canonical IDs resolved · ${fmt(identities.status_counts.unresolved||0)} unresolved · ${fmt(identities.status_counts.conflict||0)} conflicting. See id-resolution.json for recovery counts and affected rows.`:'Run the updated package command to measure canonical joins.';
  const deferred=value.deferred_seasons||[];
  $('dataset-deferred').hidden=deferred.length===0;
  $('dataset-deferred-list').innerHTML=deferred.map(item=>`<li>${esc(item.season_path)} — missing ${item.missing_inputs.map(esc).join(', ')}</li>`).join('');
  $('dataset-id-rows').innerHTML=Object.entries(value.identities||{}).map(([kind,row])=>`<tr><td>${esc(kind[0].toUpperCase()+kind.slice(1))}</td><td>${fmt(row.total)}</td>${['sofascore','fotmob','whoscored'].map(p=>`<td><strong>${fmt(row.providers[p])}</strong><small>${fmt(row.total-row.providers[p])} missing</small></td>`).join('')}<td>${fmt(row.all_three)}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">Provider linkage will appear after packaging.</td></tr>';
  $('dataset-file-rows').innerHTML=(value.files||[]).map(row=>`<tr><td>${esc(row.path)}</td><td>${fmt(row.rows)}</td><td>${fmt(row.linked_matches)}</td><td>${Object.entries(row.nonempty_fields||{}).map(([field,count])=>esc(field)+': '+fmt(count)).join(' · ')||'—'}</td></tr>`).join('');
  style();
}
for(const id of ['dataset-competition','dataset-season'])$(id).addEventListener('change',renderDataset);
if(location.pathname==='/dataset')document.querySelector('[data-tab="dataset"]').click();
