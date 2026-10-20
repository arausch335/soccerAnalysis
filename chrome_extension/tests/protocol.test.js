import test from 'node:test';
import assert from 'node:assert/strict';
import {connectionCode, sofaUrl, matchUrl, matchId} from '../protocol.js';
import {pageOperation} from '../page.js';

const token = 'x'.repeat(43);
test('connection code is limited to authenticated loopback HTTP', () => {
  assert.deepEqual(connectionCode(`http://127.0.0.1:12345/#${token}`),
    {base: 'http://127.0.0.1:12345', token});
  for (const code of [`http://evil.test:80/#${token}`, `http://127.0.0.1.evil.test:9/#${token}`,
    `http://u:p@127.0.0.1:9000/#${token}`, 'http://127.0.0.1:9000/#short',
    `http://127.0.0.1:9000/path#${token}`, `http://127.0.0.1:9000/?q=1#${token}`]) {
    assert.throws(() => connectionCode(code));
  }
});

test('only Sofascore match navigation is accepted', () => {
  assert.equal(sofaUrl('https://www.sofascore.com/'), true);
  assert.equal(matchUrl('https://www.sofascore.com/event/123'), true);
  assert.equal(matchUrl('https://www.sofascore.com/football/match/a-b/NsU#id:123'), true);
  for (const url of ['https://google.com/', 'https://www.sofascore.com.evil.test/event/123',
    'http://www.sofascore.com/event/123', 'https://www.sofascore.com/login',
    'https://www.sofascore.com/event/123?redirect=evil']) assert.equal(matchUrl(url), false);
  assert.equal(matchId('https://www.sofascore.com/football/match/a-b/NsU#id:123,tab:lineups'),123);
});

function page(text='Match', hash='#id:1') {
  globalThis.location = {origin:'https://www.sofascore.com',href:'https://www.sofascore.com/event/1',hash,pathname:'/event/1'};
  globalThis.document = {body:{innerText:text},querySelector:()=>null,querySelectorAll:()=>[]};
}

test('page reads only known resources and keeps credentials in the browser', async () => {
  page();let request;
  globalThis.fetch=async (url,args) => {
    request={url,args};
    return {status:200,text:async()=>JSON.stringify({event:{id:1}}),headers:{get:()=>null}};
  };
  const result=await pageOperation({operation:'read',endpoint:'event/1',timeout_ms:1000});
  assert.equal(result.payload.event.id,1);
  assert.equal(request.url,'/api/v1/event/1');assert.equal(request.args.credentials,'same-origin');
  for (const endpoint of ['https://evil.test','../cookies','event/1/../../settings','player/7/private']) {
    await assert.rejects(pageOperation({operation:'read',endpoint,timeout_ms:1000}));
  }
});

test('verification stops page interaction and no checkbox is clicked', async () => {
  page('Verify you are human');
  globalThis.fetch=()=>{throw new Error('Must not request during verification');};
  assert.equal((await pageOperation({operation:'read',endpoint:'event/1',timeout_ms:1000})).status,403);
});

test('HTML challenges returned with HTTP 200 require manual verification', async () => {
  page();
  globalThis.fetch=async()=>({status:200,text:async()=>'<html>challenges.cloudflare.com</html>',headers:{get:()=>null}});
  assert.equal((await pageOperation({operation:'read',endpoint:'event/1',timeout_ms:1000})).status,403);
});

test('page refuses an unrelated origin and detects manual match navigation', async () => {
  page();location.origin='https://google.com';
  await assert.rejects(pageOperation({operation:'prepare',tab:'lineups'}));
  page('Match','#id:2');
  const result=await pageOperation({operation:'prepare',tab:'lineups',match_id:1});
  assert.equal(result.status,409);
});

test('prepare handles only approved controls and paces each actual click', async () => {
  page();const clicks=[];
  const button=name=>({innerText:name,getAttribute:()=>null,getClientRects:()=>[{}],click:()=>clicks.push(name)});
  const ad=button('Close ad'), offer=button('Download now'), tab=button('Lineups');
  globalThis.getComputedStyle=()=>({visibility:'visible',display:'block'});
  document.querySelectorAll=selector=>selector==='button'?[ad,offer]:[];
  document.querySelector=()=>tab;
  const result=await pageOperation({operation:'prepare',tab:'lineups',match_id:1,delay_min_seconds:0,delay_max_seconds:0});
  assert.equal(result.tab_opened,true);assert.deepEqual(clicks,['Close ad','Lineups']);
});

test('player image receives the click rather than a substitution chooser ancestor', async () => {
  page();let imageClicks=0;
  const image={getClientRects:()=>[{}],getAttribute:()=>null,
    click:()=>imageClicks++,parentElement:{parentElement:{click:()=>assert.fail('Wrong ancestor clicked')}}};
  globalThis.getComputedStyle=()=>({visibility:'visible',display:'block'});
  document.querySelectorAll=selector=>selector.startsWith('img[')?[image]:[];
  const result=await pageOperation({operation:'prepare',tab:'lineups',match_id:1,player_id:7,
    delay_min_seconds:0,delay_max_seconds:0});
  assert.equal(imageClicks,1);assert.equal(result.diagnostics.player_target,'image');
});

test('each click samples its configured delay and reports the actual wait', async () => {
  page();
  const button=name=>({innerText:name,getAttribute:()=>null,getClientRects:()=>[{}],click:()=>{}});
  document.querySelectorAll=selector=>selector==='button'?[button('Close ad')]:[];
  document.querySelector=()=>button('Lineups');
  globalThis.getComputedStyle=()=>({visibility:'visible',display:'block'});
  const originalTimer=globalThis.setTimeout, originalRandom=Math.random;
  const random=[0,0.5];
  globalThis.setTimeout=fn=>{queueMicrotask(fn);return 0;};
  Math.random=()=>random.shift();
  try {
    const result=await pageOperation({operation:'prepare',tab:'lineups',match_id:1,
      delay_min_seconds:2,delay_max_seconds:4});
    assert.deepEqual(result.diagnostics.actions.filter(a=>a.action==='wait').map(a=>a.milliseconds),[2000,3000]);
  } finally {globalThis.setTimeout=originalTimer;Math.random=originalRandom;}
});

test('profile reads allow only birthday and transfer-history resources',async()=>{
  page();const endpoints=[];
  globalThis.fetch=async url=>{endpoints.push(url);return {status:200,text:async()=>'{"player":{"id":7}}',headers:{get:()=>null}};};
  await pageOperation({operation:'read',endpoint:'player/7',timeout_ms:1000});
  await pageOperation({operation:'read',endpoint:'player/7/transfer-history',timeout_ms:1000});
  assert.deepEqual(endpoints,['/api/v1/player/7','/api/v1/player/7/transfer-history']);
  for(const endpoint of ['player/7/account','player/7/../../settings','player/7?path=private'])await assert.rejects(pageOperation({operation:'read',endpoint,timeout_ms:1000}));
});
