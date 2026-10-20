import test from 'node:test';
import assert from 'node:assert/strict';
import {fotmobUrl} from '../protocol.js';
import {fotmobPageOperation} from '../fotmob_page.js';

test('FotMob origin and endpoints are constrained',async()=>{
  assert.equal(fotmobUrl('https://www.fotmob.com/'),true);
  assert.equal(fotmobUrl('https://www.fotmob.com.evil.test/'),false);
  globalThis.location={href:'https://www.fotmob.com/',protocol:'https:',hostname:'www.fotmob.com'};
  globalThis.fetch=()=>{throw new Error('must not fetch');};
  assert.equal((await fotmobPageOperation({endpoint:'../private',params:{}})).ok,false);
  assert.equal((await fotmobPageOperation({endpoint:'matchDetails',params:{matchId:'../1'}})).ok,false);
});

test('FotMob reads the selected-origin API and returns the complete payload',async()=>{
  globalThis.location={href:'https://www.fotmob.com/',protocol:'https:',hostname:'www.fotmob.com'};
  globalThis.fetch=async (url,options)=>{
    assert.equal(url,'/api/data/matchDetails?matchId=123');
    assert.equal(options.credentials,'same-origin');
    return {status:200,headers:{get:()=>null},text:async()=>JSON.stringify({general:{matchId:123},content:{stats:{}}})};
  };
  const result=await fotmobPageOperation({endpoint:'matchDetails',params:{matchId:123}});
  assert.equal(result.status,200);assert.equal(result.payload.general.matchId,123);
});

test('A FotMob HTML challenge is reported as verification, never JSON stats',async()=>{
  globalThis.location={href:'https://www.fotmob.com/',protocol:'https:',hostname:'www.fotmob.com'};
  globalThis.fetch=async()=>({status:200,text:async()=>'<html>verify you are human</html>'});
  const result=await fotmobPageOperation({endpoint:'matchDetails',params:{matchId:123}});
  assert.equal(result.status,403);assert.equal(result.verification,true);assert.equal(result.payload,undefined);
});
