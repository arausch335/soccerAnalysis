import test from 'node:test';
import assert from 'node:assert/strict';

let store={connected:true,paused:false,base:'http://127.0.0.1:12345',token:'x'.repeat(43),clientId:'a'.repeat(32)};
let listener, alarm, badge, requests=0;
const id='a'.repeat(32);
globalThis.chrome={
  storage:{session:{get:async()=>({...store}),set:async values=>{Object.assign(store,values);},clear:async()=>{store={};}}},
  runtime:{id,getURL:path=>`chrome-extension://${id}/${path}`,onMessage:{addListener:fn=>{listener=fn;}}},
  alarms:{onAlarm:{addListener:fn=>{alarm=fn;}},create:async()=>{},clear:async()=>{}},
  action:{setBadgeText:async values=>{badge=values.text;}},
  tabs:{query:async()=>[{id:1,url:'https://www.sofascore.com/event/1'}]}
};
globalThis.fetch=async()=>{requests++;return {ok:true,json:async()=>({phase:'closed'})};};
const {execute, navigate, checkVerification}=await import('../worker.js');
const send=(message,sender={id,url:`chrome-extension://${id}/popup.html`})=>new Promise(resolve=>listener(message,sender,resolve));

test('collector completion stops polling and reports a clear final status',async()=>{
  alarm({name:'collector'});
  const deadline=Date.now()+1000;
  while(store.connected && Date.now()<deadline) await new Promise(r=>setTimeout(r,5));
  assert.equal(store.connected,false);
  assert.match(store.message,/finished or stopped/);
  assert.equal(requests,1);assert.equal(badge,'');
});

test('a webpage cannot access the connection state or send collector commands',async()=>{
  const before=requests;
  const result=await send({action:'status'},{id,url:'https://www.sofascore.com/event/1'});
  assert.equal(result.ok,false);assert.match(result.error,/Only the extension popup/);
  assert.equal(result.token,undefined);assert.equal(requests,before);
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
  const result=await navigate({tabId:1},'https://www.sofascore.com/event/1');
  assert.equal(calls,3);assert.equal(result.diagnostics.document_id,'new');
});

function verificationState() {
  matchTab();
  store={connected:true,paused:true,tabId:1,base:'http://127.0.0.1:12345',token:'x'.repeat(43),
    clientId:'a'.repeat(32),verification:{id:'verify',args:{endpoint:'event/1/lineups'}},
    verificationReadySince:Date.now()-5000,verificationProbed:false};
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


test('first denial reloads once and rejects the previous document before checking access',async()=>{
  verificationState();store.connected=false;store.verification=null;
  let reloads=0,doc='old';const operations=[],posts=[];
  chrome.tabs.reload=async()=>{reloads++;};
  chrome.scripting={executeScript:async({args:[args]})=>{
    operations.push(args.operation);
    return [{frameId:0,documentId:doc,result:{ok:true,status:200,
      source_url:'https://www.sofascore.com/event/1',diagnostics:{match_controls:true,visible_challenge:false}}}];
  }};
  globalThis.fetch=async(url,args)=>{posts.push({url,args});return {ok:true,json:async()=>({phase:'closed'})};};
  assert.equal(await execute({...store},{id:'reload-verify',action:'verify',args:{endpoint:'event/1/lineups'}}),null);
  await new Promise(r=>setTimeout(r,0));store.connected=true;
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
  assert.ok(stages.includes('document_changed'));assert.ok(stages.includes('confirmation_read'));
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
