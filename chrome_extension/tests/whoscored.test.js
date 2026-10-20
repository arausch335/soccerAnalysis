import test from 'node:test';
import assert from 'node:assert/strict';
import {whoUrl,whoEndpoint} from '../protocol.js';
import {whoPageOperation} from '../who_page.js';

test('WhoScored access accepts only its origin and known fixture/page routes',()=>{
  assert.ok(whoUrl('https://www.whoscored.com/matches/11/live'));
  for (const url of ['https://evil.test/','https://www.whoscored.com.evil.test/','http://www.whoscored.com/','https://a:b@www.whoscored.com/']) assert.equal(whoUrl(url),false);
  assert.ok(whoEndpoint('matches/11/live','text'));
  assert.ok(whoEndpoint('Regions/252/Tournaments/2/Seasons/100/England-Premier-League','text'));
  assert.ok(whoEndpoint('Regions/250/Tournaments/12/Seasons/7804/Stages/18065/Show/europe-champions-league-2019-2020','text'));
  assert.ok(whoEndpoint('Regions/250/Tournaments/12/Seasons/7804','text'));
  assert.ok(whoEndpoint('tournaments/11/data/','json',{d:'202408',isAggregate:'false'}));
  for(const endpoint of ['../account','matches/11/live?url=evil','https://evil.test/','regions/1/tournaments/1/../../account','matches/11/live#x']) assert.equal(whoEndpoint(endpoint,'text'),false);
  assert.equal(whoEndpoint('tournaments/11/data/','json',{d:'202408',isAggregate:'false',url:'evil'}),false);
});

function page(text='Match') {
  globalThis.location={origin:'https://www.whoscored.com',href:'https://www.whoscored.com/matches/11/live',pathname:'/matches/11/live'};
  globalThis.document={body:{innerText:text},documentElement:{outerHTML:'<html>matchCentreData: {"events":[]}</html>'},querySelector:()=>null};
}

test('WhoScored reads the page data and leaves verification to the user',async()=>{
  page();globalThis.fetch=()=>assert.fail('Match event data should come from the loaded page');
  const result=await whoPageOperation({kind:'text'});
  assert.equal(result.status,200);assert.match(result.text,/matchCentreData/);
  page('Pardon our interruption');assert.equal((await whoPageOperation({kind:'text'})).status,403);
  page();location.origin='https://evil.test';await assert.rejects(whoPageOperation({kind:'text'}));
});

test('fixture requests use only the current stage, retain browser cookies and propagate rate limits',async()=>{
  page();location.pathname='/stages/11/fixtures';let request;
  globalThis.fetch=async(url,args)=>{request={url,args};return {status:200,text:async()=>'{"tournaments":[]}',headers:{get:()=>null}};};
  const args={kind:'json',endpoint:'tournaments/11/data/',params:{d:'202408',isAggregate:'false'}};
  assert.deepEqual((await whoPageOperation(args)).payload,{tournaments:[]});
  assert.equal(request.args.credentials,'same-origin');assert.match(request.url,/^\/tournaments\/11\/data\/\?/);
  globalThis.fetch=async()=>({status:429,text:async()=>'',headers:{get:()=> '900'}});
  const denied=await whoPageOperation(args);assert.equal(denied.status,429);assert.equal(denied.retryAfter,'900');
  location.pathname='/stages/12/fixtures';await assert.rejects(whoPageOperation(args));
});

test('WhoScored profile lookups are constrained to public player history',()=>{
  assert.ok(whoEndpoint('players/463353/history','text'));
  for(const endpoint of ['players/463353/account','players/463353/history?url=evil','players/../history'])assert.equal(whoEndpoint(endpoint,'text'),false);
});
