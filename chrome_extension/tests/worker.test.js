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
await import('../worker.js');
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
