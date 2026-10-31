import test from 'node:test';
import assert from 'node:assert/strict';

let store={connected:true,paused:false,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32)};
let listener, alarm, badge, startup, installed, persistent={}, requests=0;
const id='a'.repeat(32);
globalThis.chrome={
  storage:{local:{get:async()=>({...persistent}),set:async v=>Object.assign(persistent,v),remove:async key=>{delete persistent[key];}},session:{get:async()=>({...store}),set:async values=>{Object.assign(store,values);},clear:async()=>{store={};}}},
  runtime:{onStartup:{addListener:fn=>{startup=fn;}},onInstalled:{addListener:fn=>{installed=fn;}},id,getURL:path=>`chrome-extension://${id}/${path}`,onMessage:{addListener:fn=>{listener=fn;}}},
  alarms:{onAlarm:{addListener:fn=>{alarm=fn;}},create:async()=>{},clear:async()=>{}},
  action:{setBadgeText:async values=>{badge=values.text;}},
  tabs:{query:async()=>[{id:1,url:'https://www.sofascore.com/event/1'}]}
};
globalThis.fetch=async()=>{requests++;return {ok:true,json:async()=>({phase:'closed'})};};
const {execute, navigate, checkVerification}=await import('../worker.js');
const send=(message,sender={id,url:`chrome-extension://${id}/popup.html`})=>new Promise(resolve=>listener(message,sender,resolve));

test('a Python restart preserves the connection and retries automatically',async()=>{
  alarm({name:'collector'});
  const deadline=Date.now()+1000;
  while(!store.reconnecting && Date.now()<deadline) await new Promise(r=>setTimeout(r,5));
  assert.equal(store.connected,true);assert.equal(store.reconnecting,true);
  assert.match(store.message,/reconnect automatically/);
  assert.equal(requests,1);assert.equal(store.token,'x'.repeat(43));store.connected=false;
});

test('a webpage cannot access the connection state or send collector commands',async()=>{
  const before=requests;
  const result=await send({action:'status'},{id,url:'https://www.sofascore.com/event/1'});
  assert.equal(result.ok,false);assert.match(result.error,/Only the extension popup/);
  assert.equal(result.token,undefined);assert.equal(requests,before);
});

test('disconnect clears incomplete saved state and returns even when Python never responds',async()=>{
  for(const clientId of [undefined,'a'.repeat(32)]) {
    store={connected:true,reconnecting:true,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId};
    persistent={collectorConnection:{...store}};
    globalThis.fetch=async()=>new Promise(()=>{});
    const result=await Promise.race([send({action:'disconnect'}),new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),100))]);
    assert.equal(result.ok,true);assert.equal(store.connected,false);
    assert.equal(store.clientId,undefined);assert.equal(persistent.collectorConnection,undefined);
    assert.equal(badge,'');
    assert.equal((await send({action:'status'})).connected,false);
  }
  store={connected:false,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32)};
});

test('connect refuses any active tab outside Sofascore',async()=>{
  chrome.tabs.query=async()=>[{id:1,url:'https://accounts.google.com/'}];
  const before=requests;
  const result=await send({action:'connect',code:`http://127.0.0.1:12345/#${'x'.repeat(43)}`});
  assert.equal(result.ok,false);assert.match(result.error,/Open Sofascore/);
  assert.equal(requests,before);
});

test('pause and resume tell Python to suspend and restore the command deadline',async()=>{
  store.connected=true;store.paused=false;store.tabId=1;
  chrome.tabs.get=async()=>({id:1,url:'https://www.sofascore.com/event/1'});
  const paths=[];
  globalThis.fetch=async url=>{paths.push(url);return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal((await send({action:'pause'})).ok,true);assert.equal(store.paused,true);
  assert.equal((await send({action:'resume'})).ok,true);
  assert.ok(paths.some(p=>p.endsWith('/v1/pause')));
  assert.ok(paths.some(p=>p.endsWith('/v1/resume')));
  await new Promise(r=>setTimeout(r,10));
});

function matchTab() {
  let url='https://www.sofascore.com/event/1';
  chrome.tabs.get=async()=>({id:1,url,status:'loading'});
  chrome.tabs.update=async(_id, changes)=>{url=changes.url || url;};
  chrome.tabs.reload=async()=>{};
}

test('navigation accepts usable match content while background resources still load',async()=>{
  matchTab();
  chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,
    source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true}}}]};
  const result=await navigate({tabId:1},'https://www.sofascore.com/event/1');
  assert.equal(result.ok,true);assert.equal(result.diagnostics.browser_load_state,'loading');
});

test('an empty page result reopens the intended match and retries once',async()=>{
  matchTab();let reads=0, inspections=0;
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='inspect') {
      inspections++;
      return [{frameId:0,result:{ok:true,source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true}}}];
    }
    reads++;
    return [{frameId:0,result:reads===1 ? undefined : {ok:true,status:200,payload:{statistics:{}}}}];
  }};
  const result=await execute({tabId:1},{id:'read',action:'page',args:{operation:'read',endpoint:'event/1/player/7/statistics'}});
  assert.equal(reads,2);assert.equal(inspections,2);
  assert.equal(result.status,200);assert.equal(result.diagnostics.recovery_attempts,1);
  assert.match(result.diagnostics.recovery_reason,/no collection data/);
});

test('HTTP denial and rate limit reads are handed to Python without an immediate retry',async()=>{
  matchTab();
  for(const status of [403,429]) {
    let calls=0;
    chrome.scripting={executeScript:async()=>{calls++;return [{frameId:0,result:{ok:true,status}}];}};
    const result=await execute({tabId:1},{id:'denied',action:'page',args:{operation:'read',endpoint:'event/1'}});
    assert.equal(result.status,status);assert.equal(calls,1);
  }
});

test('a second empty page result ends recovery instead of looping',async()=>{
  matchTab();let reads=0;
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='inspect') return [{frameId:0,result:{ok:true,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true}}}];
    reads++;return [{frameId:0}];
  }};
  const result=await execute({tabId:1},{id:'empty',action:'page',args:{operation:'read',endpoint:'event/1'}});
  assert.equal(reads,2);assert.equal(result.ok,false);assert.equal(result.diagnostics.recovery_attempts,1);
});

test('reload readiness rejects the old document even when its match controls remain visible',async()=>{
  matchTab();let calls=0;
  chrome.scripting={executeScript:async()=>{
    calls++;
    return [{frameId:0,documentId:calls<3?'old':'new',result:{ok:true,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true}}}];
  }};
  const result=await navigate({tabId:1},'https://www.sofascore.com/event/1',true);
  assert.equal(calls,3);assert.equal(result.diagnostics.document_id,'new');
});

function verificationState() {
  matchTab();
  store={connected:true,paused:true,tabId:1,base:'http://127.0.0.1:12345',token:'x'.repeat(43),
    clientId:'a'.repeat(32),verification:{id:'verify',args:{endpoint:'event/1/lineups'}},
    verificationReadySince:Date.now()-5000,verificationProbed:false,verificationReloads:1};
}

test('verification resumes automatically after settled match controls and successful access',async()=>{
  verificationState();const operations=[],posts=[];
  chrome.scripting={executeScript:async({args:[args]})=>{
    operations.push(args.operation);
    return [{frameId:0,result:{ok:true,status:200,source_url:'https://www.sofascore.com/event/1',
      diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  globalThis.fetch=async(url,args)=>{posts.push({url,args});return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal(await checkVerification({...store}),true);
  assert.deepEqual(operations,['inspect','read']);
  assert.equal(store.verification,null);
  const result=JSON.parse(posts.find(p=>p.url.endsWith('/result')).args.body).result;
  assert.equal(result.diagnostics.verification_resolution,'automatic');
  await new Promise(r=>setTimeout(r,10));
});

test('visible verification is observed without clicks or data requests',async()=>{
  verificationState();
  chrome.scripting={executeScript:async({args:[args]})=>{
    assert.equal(args.operation,'inspect');
    return [{frameId:0,result:{ok:true,source_url:'https://www.sofascore.com/event/1',diagnostics:{visible_challenge:true}}}];
  }};
  assert.equal(await checkVerification({...store}),false);
  assert.equal(store.paused,true);assert.equal(store.verificationReadySince,null);
});

test('a persistent denied request remains paused and is not repeatedly probed',async()=>{
  verificationState();let reads=0;
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='read')reads++;
    return [{frameId:0,result:{ok:true,status:403,source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  assert.equal(await checkVerification({...store}),false);
  assert.equal(await checkVerification({...store}),false);
  assert.equal(reads,1);assert.equal(store.paused,true);
  assert.match(store.message,/waiting, not stopped/);
  store.connected=false;store.verification=null;
});

test('manual Resume still completes a verification that did not clear automatically',async()=>{
  verificationState();const posts=[];
  globalThis.fetch=async(url,args)=>{posts.push({url,args});return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal((await send({action:'resume'})).ok,true);
  assert.equal(store.verification,null);
  const result=JSON.parse(posts.find(p=>p.url.endsWith('/result')).args.body).result;
  assert.equal(result.diagnostics.verification_resolution,'manual');
  await new Promise(r=>setTimeout(r,10));
});


test('first denial schedules a real reload after one minute and rejects the previous document',async()=>{
  verificationState();store.connected=false;store.verification=null;
  let reloads=0,doc='old';const operations=[],posts=[];
  chrome.tabs.reload=async(_id,options)=>{assert.deepEqual(options,{bypassCache:true});reloads++;};
  chrome.scripting={executeScript:async({args:[args]})=>{
    operations.push(args.operation);
    return [{frameId:0,documentId:doc,result:{ok:true,status:200,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  globalThis.fetch=async(url,args)=>{posts.push({url,args});return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal(await execute({...store},{id:'reload-verify',action:'verify',args:{endpoint:'event/1/lineups'}}),null);
  await new Promise(r=>setTimeout(r,0));store.connected=true;
  assert.equal(reloads,0);
  assert.ok(store.verificationNextReloadAt-Date.now()>59000);
  await checkVerification({...store});
  assert.equal(reloads,0);
  store.verificationNextReloadAt=Date.now()-1;
  await checkVerification({...store});
  assert.equal(reloads,1);
  await checkVerification({...store});
  assert.equal(operations.includes('read'),false);
  doc='new';await checkVerification({...store});
  assert.equal(operations.includes('read'),false);
  store.verificationReadySince=Date.now()-5000;
  assert.equal(await checkVerification({...store}),true);
  assert.equal(reloads,1);assert.equal(operations.filter(x=>x==='read').length,1);
  const stages=posts.filter(p=>p.url.endsWith('/diagnostic')).map(p=>JSON.parse(p.args.body).result.recovery_stage);
  assert.ok(stages.includes('reload_started'));assert.ok(stages.includes('waiting_for_reload'));
  assert.ok(stages.includes('reload_confirmed'));assert.ok(stages.includes('confirmation_read'));
  await new Promise(r=>setTimeout(r,10));
});

test('schedule recovery accepts a ready homepage without match controls',async()=>{
  verificationState();store.verification.args.endpoint='unique-tournament/8/season/42409/events/last/0';
  chrome.tabs.get=async()=>({id:1,url:'https://www.sofascore.com/'});
  chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,status:200,
    source_url:'https://www.sofascore.com/',diagnostics:{match_controls:false,ready_state:'complete',visible_challenge:false}}}]};
  assert.equal(await checkVerification({...store}),true);
  await new Promise(r=>setTimeout(r,10));
});

test('match recovery never reads from the wrong match and a manual reload permits one fresh check',async()=>{
  verificationState();store.verificationDocument='old';store.verificationProbed=true;
  let mid=2,doc='old',reads=0;
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='read')reads++;
    return [{frameId:0,documentId:doc,result:{ok:true,status:403,
      source_url:`https://www.sofascore.com/event/${mid}`,diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  await checkVerification({...store});assert.equal(reads,0);
  mid=1;doc='manual-reload';await checkVerification({...store});
  assert.equal(store.verificationProbed,false);assert.equal(reads,0);
  store.verificationReadySince=Date.now()-5000;
  await checkVerification({...store});await checkVerification({...store});
  assert.equal(reads,1);assert.equal(store.paused,true);
  store.connected=false;store.verification=null;
});


test('three reloads use 1, 5 and 30 minute waits and then require explicit Resume',async()=>{
  verificationState();store.connected=false;store.verification=null;
  const realNow=Date.now;let now=100000,doc='first',reloads=0,reads=0;const alarms=[];
  Date.now=()=>now;
  chrome.alarms.create=async(name,options)=>alarms.push({name,...options});
  chrome.tabs.reload=async(_id,options)=>{assert.deepEqual(options,{bypassCache:true});reloads++;doc='reload-'+reloads;};
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='read')reads++;
    return [{frameId:0,documentId:doc,result:{ok:true,status:403,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  try {
    await execute({...store},{id:'three',action:'verify',args:{endpoint:'event/1/lineups'}});
    await new Promise(r=>setTimeout(r,0));store.connected=true;
    assert.equal(store.verificationNextReloadAt,now+60000);
    for(let attempt=1;attempt<=3;attempt++) {
      now=store.verificationNextReloadAt-1;
      await checkVerification({...store});assert.equal(reloads,attempt-1);
      now++;await checkVerification({...store});assert.equal(reloads,attempt);
      if(attempt<3)assert.equal(store.verificationNextReloadAt,now+[0,300000,1800000][attempt]);
      await checkVerification({...store});now+=5000;await checkVerification({...store});
      assert.equal(reads,attempt);
    }
    assert.equal(store.verificationNeedsApproval,true);assert.equal(store.paused,true);
    assert.match(store.message,/approval/);assert.equal(store.verification.id,'three');
    now+=3600000;await checkVerification({...store});assert.equal(reloads,3);assert.equal(reads,3);
    assert.equal(alarms.filter(a=>a.name==='verification-reload').length,4);
  } finally {Date.now=realNow;store.connected=false;store.verification=null;}
});

test('missing new document cannot confirm access and still advances to the next reload',async()=>{
  verificationState();store.verificationDocument='old';store.verificationReloads=1;
  store.verificationAwaitingDocument=true;store.verificationNextReloadAt=Date.now()+300000;
  let reads=0,reloads=0;
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='read')reads++;
    return [{frameId:0,documentId:'old',result:{ok:true,status:200,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true}}}];
  }};
  chrome.tabs.reload=async()=>{reloads++;};
  await checkVerification({...store});assert.equal(reads,0);
  store.verificationNextReloadAt=Date.now()-1;
  await checkVerification({...store});assert.equal(reloads,1);assert.equal(store.verificationReloads,2);
  assert.equal(reads,0);store.connected=false;store.verification=null;
});

test('third reload eventually requests approval if the page stays challenged',async()=>{
  verificationState();store.verificationReloads=3;store.verificationFinalDeadline=Date.now()+60000;
  chrome.scripting={executeScript:async({args:[args]})=>{
    assert.equal(args.operation,'inspect');
    return [{frameId:0,result:{ok:true,diagnostics:{visible_challenge:true}}}];
  }};
  await checkVerification({...store});assert.equal(store.verificationNeedsApproval,undefined);
  store.verificationFinalDeadline=Date.now()-1;
  await checkVerification({...store});assert.equal(store.verificationNeedsApproval,true);
  assert.equal(store.paused,true);store.connected=false;store.verification=null;
});


test('a stalled page inspection cannot prevent a due full reload',async()=>{
  verificationState();store.verificationNextReloadAt=Date.now()-1;
  let reloads=0;
  chrome.scripting={executeScript:async()=>new Promise(()=>{})};
  chrome.tabs.reload=async(_id,options)=>{assert.deepEqual(options,{bypassCache:true});reloads++;};
  await checkVerification({...store});
  assert.equal(reloads,1);assert.equal(store.verificationReloads,2);
  store.connected=false;store.verification=null;
});

test('a Chrome alarm resumes a saved due recovery without a new Python command',async()=>{
  verificationState();store.verificationNextReloadAt=Date.now()-1;
  let reloads=0;
  chrome.scripting={executeScript:async()=>[{frameId:0,documentId:'old',result:{ok:true,diagnostics:{}}}]};
  chrome.tabs.reload=async()=>{reloads++;};
  await alarm({name:'verification-reload'});
  const deadline=Date.now()+1000;
  while(!reloads && Date.now()<deadline)await new Promise(r=>setTimeout(r,5));
  assert.equal(reloads,1);assert.equal(store.connected,true);
  store.connected=false;store.verification=null;
});


test('verification redirect losing the match hash restores the event before retrying automatically',async()=>{
  verificationState();
  const bare='https://www.sofascore.com/football/match/newcastle-united-brighton-and-hove-albion/FsO';
  const target=bare+'#id:10385743';let url=bare,reads=0;const updates=[],posts=[];
  store.verification.args.endpoint='event/10385743/player/560128/statistics';
  store.verificationMatchUrl=target;store.verificationDocument='returned';
  store.verificationNextReloadAt=Date.now()+300000;
  chrome.tabs.get=async()=>({id:1,url});
  chrome.tabs.update=async(_id,changes)=>{updates.push(changes.url);};
  chrome.scripting={executeScript:async({args:[args]})=>{
    if(args.operation==='read')reads++;
    return [{frameId:0,documentId:'returned',result:{ok:true,status:200,source_url:url,
      diagnostics:{match_controls:true,ready_state:'complete',visible_challenge:false}}}];
  }};
  globalThis.fetch=async(url,args)=>{posts.push({url,args});return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal(await checkVerification({...store}),false);
  assert.deepEqual(updates,[target]);assert.equal(reads,0);
  // A stale observation while the hash update is pending must not loop navigation.
  await checkVerification({...store});assert.deepEqual(updates,[target]);
  url=target;await checkVerification({...store});assert.equal(reads,0);
  store.verificationReadySince=Date.now()-5000;
  assert.equal(await checkVerification({...store}),true);assert.equal(reads,1);
  const stages=posts.filter(p=>p.url.endsWith('/diagnostic')).map(p=>JSON.parse(p.args.body).result.recovery_stage);
  assert.ok(stages.includes('match_restore_requested'));assert.ok(stages.includes('match_restored'));
  const result=JSON.parse(posts.find(p=>p.url.endsWith('/result')).args.body).result;
  assert.equal(result.diagnostics.verification_resolution,'automatic');
  assert.equal(store.verificationNextReloadAt,null);
  await new Promise(r=>setTimeout(r,10));
});

test('restoration waits for verification and never selects a different historical match',async()=>{
  verificationState();store.verificationDocument='captcha';
  store.verificationMatchUrl='https://www.sofascore.com/event/999';
  let challenged=true,url='https://www.sofascore.com/captcha.html';const updates=[];
  chrome.tabs.get=async()=>({id:1,url});
  chrome.tabs.update=async(_id,changes)=>updates.push(changes.url);
  chrome.scripting={executeScript:async({args:[args]})=>{
    assert.equal(args.operation,'inspect');
    return [{frameId:0,documentId:'captcha',result:{ok:true,source_url:url,
      diagnostics:{match_controls:false,ready_state:'complete',visible_challenge:challenged}}}];
  }};
  await checkVerification({...store});assert.equal(updates.length,0);
  challenged=false;await checkVerification({...store});assert.equal(updates.length,0);
  url='https://www.sofascore.com/';await checkVerification({...store});
  assert.deepEqual(updates,['https://www.sofascore.com/event/1']);
  store.connected=false;store.verification=null;
});


test('preparing the already verified match preserves its document without another reload',async()=>{
  matchTab();let reloads=0,updates=0;
  chrome.tabs.reload=async()=>{reloads++;};chrome.tabs.update=async()=>{updates++;};
  chrome.scripting={executeScript:async()=>[{frameId:0,documentId:'verified',result:{ok:true,
    source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true,visible_challenge:false}}}]};
  const result=await navigate({tabId:1},'https://www.sofascore.com/event/1');
  assert.equal(result.diagnostics.navigation_reused,true);
  assert.equal(result.diagnostics.document_id,'verified');assert.equal(reloads,0);assert.equal(updates,0);
});

test('WhoScored navigates only the selected tab and returns the intended document',async()=>{
  let url='https://www.sofascore.com/event/1';const navigations=[];
  chrome.tabs.get=async()=>({id:1,url,status:'complete'});
  chrome.tabs.update=async(id,args)=>{assert.equal(id,1);url=args.url;navigations.push(url);};
  chrome.scripting={executeScript:async({target,func,args})=>{
    assert.equal(target.tabId,1);assert.equal(func.name,'whoPageOperation');
    return [{frameId:0,result:{ok:true,status:200,source_url:url,text:'matchId: 11, matchCentreData: {}'}}];
  }};
  const result=await execute({tabId:1},{id:'who',action:'whoscored',args:{kind:'text',endpoint:'matches/11/live',params:{}}});
  assert.equal(result.source_url,'https://www.whoscored.com/matches/11/live');
  assert.deepEqual(navigations,['https://www.whoscored.com/matches/11/live']);
  await assert.rejects(execute({tabId:1},{id:'evil',action:'whoscored',args:{kind:'text',endpoint:'../settings',params:{}}}));
});

test('one chosen tab switches from WhoScored to FotMob and back to a Sofascore schedule',async()=>{
  let url='https://www.whoscored.com/matches/123/live';const moves=[];
  chrome.tabs.get=async()=>({id:1,url,status:'loading'});
  chrome.tabs.update=async(_id,changes)=>{url=changes.url || url;moves.push(url);};
  chrome.scripting.executeScript=async request=>{
    if (request.func.name==='fotmobPageOperation') return [{frameId:0,result:{ok:true,status:200,source_url:url,payload:{general:{matchId:123}}}}];
    if (request.func.name==='pageOperation') return [{frameId:0,result:{ok:true,status:200,source_url:url,payload:{seasons:[]}}}];
    return [{frameId:0,result:'interactive'}];
  };
  const s={...store,tabId:1};
  const fot=await execute(s,{id:'fot',action:'fotmob',args:{endpoint:'matchDetails',params:{matchId:123}}});
  assert.equal(fot.status,200);assert.equal(fot.source_url,'https://www.fotmob.com/');
  const sofa=await execute(s,{id:'sofa',action:'page',args:{operation:'read',endpoint:'unique-tournament/17/seasons'}});
  assert.equal(sofa.status,200);assert.equal(sofa.source_url,'https://www.sofascore.com/');
  assert.deepEqual(moves,['https://www.fotmob.com/','https://www.sofascore.com/']);
});

test('WhoScored fixtures use the published season route and reject mismatched stage URLs',async()=>{
  let url='https://www.sofascore.com/';const moves=[];
  chrome.tabs.get=async()=>({id:1,url,status:'complete'});
  chrome.tabs.update=async(id,args)=>{url=args.url;moves.push(url);};
  chrome.scripting.executeScript=async()=>[{frameId:0,result:{ok:true,status:200,source_url:url,payload:{tournaments:[]}}}];
  const args={kind:'json',endpoint:'tournaments/4345/data/',params:{d:'201009',isAggregate:'false'},
    page_path:'regions/252/tournaments/2/seasons/2458/stages/4345/fixtures/england-premier-league-2010-2011'};
  const result=await execute({tabId:1},{id:'fixture-route',action:'whoscored',args});
  assert.equal(result.status,200);
  assert.deepEqual(moves,['https://www.whoscored.com/'+args.page_path]);
  for(const page_path of [undefined,'stages/4345/fixtures',args.page_path.replace('/4345/','/4346/'),'https://evil.test/']) {
    await assert.rejects(execute({tabId:1},{id:'bad-fixture-route',action:'whoscored',args:{...args,page_path}}));
  }
  assert.equal(moves.length,1);
});

test('WhoScored missing pages return 404 promptly instead of timing out',async()=>{
  chrome.tabs.get=async()=>({id:1,url:'https://www.whoscored.com/404.html?aspxerrorpath=/missing',status:'complete'});
  chrome.tabs.update=async()=>{};
  chrome.scripting.executeScript=async()=>assert.fail('Do not scrape a 404 document');
  const result=await execute({tabId:1},{id:'who-missing',action:'whoscored',args:{kind:'text',endpoint:'regions/252/tournaments/2/england-premier-league'}});
  assert.equal(result.status,404);assert.equal(result.reason,'page_not_found');
});

test('WhoScored waits past null match-data initialization for the actual object',async()=>{
  const url='https://www.whoscored.com/matches/11/live';let reads=0;
  chrome.tabs.get=async()=>({id:1,url,status:'complete'});
  chrome.scripting={executeScript:async()=>{
    reads++;
    return [{frameId:0,result:{ok:true,status:200,source_url:url,
      text:reads===1?'matchId: 11, matchCentreData: null':'matchId: 11, matchCentreData: {"events":[]}'}}];
  }};
  const result=await execute({tabId:1},{id:'who-ready',action:'whoscored',args:{kind:'text',endpoint:'matches/11/live',params:{},manual_wait_ms:1500}});
  assert.equal(reads,2);assert.equal(result.status,200);assert.match(result.text,/\{"events"/);
});

test('WhoScored local data deadline is reported as an extension timeout, not an HTTP response',async()=>{
 const url='https://www.whoscored.com/matches/11/live';
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});
 chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,status:200,source_url:url,text:'matchId: 11, matchCentreData: null'}}]};
 const now=Date.now;let elapsed=0;
 try {
  Date.now=()=>now()+(elapsed+=500);
  const result=await execute({tabId:1},{id:'who-timeout',action:'whoscored',args:{kind:'text',endpoint:'matches/11/live',params:{},timeout_ms:1000,manual_wait_ms:120000}});
  assert.equal(result.reason,'extension_timeout');assert.equal(result.status,null);
  assert.ok(elapsed<120000);
 } finally {Date.now=now;}
});

test('an unresolved WhoScored challenge remains verification-required rather than missing data',async()=>{
 const url='https://www.whoscored.com/matches/11/live';
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});
 chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,status:403,verification:true,source_url:url}}]};
 const now=Date.now;let elapsed=0;
 try {
  Date.now=()=>now()+(elapsed+=500);
  const result=await execute({tabId:1},{id:'who-verify',action:'whoscored',args:{kind:'text',endpoint:'matches/11/live',params:{},timeout_ms:1000,manual_wait_ms:1000}});
  assert.equal(result.reason,'verification_required');assert.equal(result.status,403);assert.equal(result.verification,true);
 } finally {Date.now=now;}
});


test('temporary network failure retains credentials and authenticates again on the next alarm',async()=>{
  persistent={};store={connected:true,paused:false,tabId:1,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32)};
  globalThis.fetch=async()=>{throw new TypeError('Failed to fetch');};
  await alarm({name:'collector'});
  for(let i=0;i<50&&!store.reconnecting;i++)await new Promise(r=>setTimeout(r,5));
  assert.equal(store.connected,true);assert.equal(store.reconnecting,true);
  const paths=[];chrome.tabs.get=async()=>({id:1,url:'https://www.sofascore.com/event/1'});
  globalThis.fetch=async url=>{paths.push(url);return {ok:true,json:async()=>({phase:'closed'})};};
  await alarm({name:'collector'});
  for(let i=0;i<50&&!paths.length;i++)await new Promise(r=>setTimeout(r,5));
  assert.ok(paths.some(p=>p.endsWith('/connect')));store.connected=false;
});

test('Chrome startup restores a saved connection and recreates a closed provider tab',async()=>{
  store={};persistent={collectorConnection:{base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32),tabId:99,tabUrl:'https://www.whoscored.com/'}};
  chrome.tabs.get=async()=>{throw new Error('Tab missing');};chrome.tabs.create=async options=>({id:7,url:options.url});
  const paths=[];globalThis.fetch=async url=>{paths.push(url);return {ok:true,json:async()=>({phase:'closed'})};};
  await startup();for(let i=0;i<50&&!paths.length;i++)await new Promise(r=>setTimeout(r,5));
  assert.equal(store.tabId,7);assert.equal(store.token,'x'.repeat(43));assert.ok(paths.some(p=>p.endsWith('/connect')));
  await send({action:'disconnect'});assert.equal(store.connected,false);assert.equal(persistent.collectorConnection,undefined);
  await startup();assert.equal(store.connected,false);
});


test('extension reload announces profile capabilities with an existing session',async()=>{
  store={connected:true,paused:false,reconnecting:false,tabId:1,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32)};
  chrome.tabs.get=async()=>({id:1,url:'https://www.sofascore.com/'});
  const connections=[];
  globalThis.fetch=async(url,args)=>{if(url.endsWith('/connect'))connections.push(JSON.parse(args.body));return {ok:true,json:async()=>({phase:'closed'})};};
  await installed();for(let i=0;i<50&&!connections.length;i++)await new Promise(r=>setTimeout(r,5));
  assert.deepEqual(connections[0].capabilities,['player-profiles-v1']);
  assert.equal(connections[0].client_id,'a'.repeat(32));store.connected=false;
});


test('WhoScored profile keeps birthday evidence when History has no rendered career rows',async()=>{
 const url='https://www.whoscored.com/players/347070/history/bohdan-liedniev';
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});chrome.tabs.update=async()=>{};
 const text='<h1 class="header-name">Bohdan Liedniev</h1><img class="player-picture" src="/players/347070.jpg">Age: 28 years old (07-04-1998)';
 chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,status:200,source_url:url,text}}]};
 const now=Date.now;let elapsed=0;
 try {
  Date.now=()=>now()+(elapsed+=500);
  const result=await execute({tabId:1},{id:'who-profile-partial',action:'whoscored',args:{kind:'text',endpoint:'players/347070/history',timeout_ms:1000}});
  assert.equal(result.status,200);assert.equal(result.profile_partial,true);assert.match(result.text,/07-04-1998/);
 } finally {Date.now=now;}
});

test('WhoScored profile never retains a different player header',async()=>{
 const url='https://www.whoscored.com/players/347070/history';
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});
 chrome.scripting={executeScript:async()=>[{frameId:0,result:{ok:true,status:200,source_url:url,text:'<h1 class="header-name">Other</h1><img class="player-picture" src="/players/99.jpg">'}}]};
 const now=Date.now;let elapsed=0;
 try {
  Date.now=()=>now()+(elapsed+=500);
  const result=await execute({tabId:1},{id:'who-profile-wrong',action:'whoscored',args:{kind:'text',endpoint:'players/347070/history',timeout_ms:1000}});
  assert.equal(result.reason,'extension_timeout');assert.equal(result.status,null);
 } finally {Date.now=now;}
});

test('WhoScored directory loading responses are polled until selectors are ready',async()=>{
 const endpoint='regions/250/tournaments/30/seasons/6843/europe-europa-league';
 const url='https://www.whoscored.com/'+endpoint;let reads=0;
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});
 chrome.scripting.executeScript=async()=>[{frameId:0,result:++reads===1?
  {ok:true,status:202,reason:'directory_loading',source_url:url}:
  {ok:true,status:200,text:'<select id="stages"><option>Group A</option></select>',source_url:url}}];
 const result=await execute({tabId:1},{id:'directory-ready',action:'whoscored',args:{kind:'text',endpoint}});
 assert.equal(result.status,200);assert.equal(reads,2);
});


test('WhoScored retries a detached frame without reading another document',async()=>{
 let url='https://www.whoscored.com/matches/11/live',attempts=0;
 chrome.tabs.get=async()=>({id:1,url,status:'complete'});
 chrome.tabs.update=async(id,args)=>{url=args.url;};
 chrome.scripting={executeScript:async()=>{
   if(++attempts===1)throw new Error('Frame with ID 0 was removed.');
   return [{frameId:0,result:{ok:true,status:200,source_url:url,text:'matchId: 11, matchCentreData: {}'}}];
 }};
 const result=await execute({tabId:1},{id:'retry-frame',action:'whoscored',args:{kind:'text',endpoint:'matches/11/live'}});
 assert.equal(result.status,200);assert.equal(attempts,2);
});
