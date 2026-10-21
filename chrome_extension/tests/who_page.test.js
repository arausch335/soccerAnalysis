import test from 'node:test';
import assert from 'node:assert/strict';
import {whoPageOperation} from '../who_page.js';
function page(text,visible) {
 globalThis.location={origin:'https://www.whoscored.com',href:'https://www.whoscored.com/matches/11/live'};
 globalThis.document={readyState:'complete',body:{innerText:visible},documentElement:{outerHTML:text},querySelector:()=>null};
}
const args={kind:'text',endpoint:'matches/11/live'};
test('explicit missing stats on the identified match returns match-local unavailable',async()=>{
 page('matchId: 11, matchCentreData: null','No statistics available for this match');
 const result=await whoPageOperation(args);assert.equal(result.status,404);assert.equal(result.reason,'match_data_unavailable');
});
test('a loading match with no data object is not classified as unavailable',async()=>{
 page('matchId: 11, matchCentreData: null','Tottenham 0–3 Chelsea');
 assert.equal((await whoPageOperation(args)).status,200);
 document.body.innerText='No statistics available';document.readyState='loading';
 assert.equal((await whoPageOperation(args)).status,200);
});
test('missing stats text for a different match or beside actual data cannot suppress data',async()=>{
 page('matchId: 12, matchCentreData: null','No statistics available');
 assert.equal((await whoPageOperation(args)).status,200);
 page('matchId: 11, matchCentreData: {"events":[]}','No statistics available');
 assert.equal((await whoPageOperation(args)).status,200);
});
test('verification takes precedence over missing coverage text',async()=>{
 page('matchId: 11, matchCentreData: null','Verify you are human. No statistics available');
 const result=await whoPageOperation(args);assert.equal(result.status,403);assert.equal(result.verification,true);
});
