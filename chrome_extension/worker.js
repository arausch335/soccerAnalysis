import {connectionCode, sofaUrl, matchUrl, matchId, whoUrl, whoEndpoint, fotmobUrl} from './protocol.js';
import {pageOperation} from './page.js';
import {fotmobPageOperation} from './fotmob_page.js';
import {whoPageOperation} from './who_page.js';

let polling = false;
let controller;
let verifying = false;
let resolvingVerification = false;
const RELOAD_DELAYS = [60000, 300000, 1800000];
const RELOAD_ALARM = 'verification-reload';
const state = async () => {
  const s = await chrome.storage.session.get(null);
  if (s.base || !chrome.storage.local) return s;
  const {collectorConnection:saved} = await chrome.storage.local.get('collectorConnection');
  if (!saved) return s;
  const restored={...saved,connected:true,paused:!!saved.paused,reconnecting:true,message:'Restoring the saved Python connection…'};
  await chrome.storage.session.set(restored);
  return restored;
};
const persistConnection = async s => {
  if (chrome.storage.local) await chrome.storage.local.set({collectorConnection:{base:s.base,token:s.token,clientId:s.clientId,tabId:s.tabId,tabUrl:s.tabUrl,paused:!!s.paused}});
};
const save = values => chrome.storage.session.set(values);
const badge = text => chrome.action.setBadgeText({text});

async function api(s, path, value) {
  if (path === 'connect') value={...value,capabilities:['player-profiles-v1']};
  const response = await fetch(s.base + '/v1/' + path, {
    method: value === undefined ? 'GET' : 'POST',
    headers: {Authorization: 'Bearer ' + s.token, 'X-Collector-Client': s.clientId,
      ...(value === undefined ? {} : {'Content-Type': 'application/json'})},
    body: value === undefined ? undefined : JSON.stringify(value),
    signal: path === 'diagnostic' ? AbortSignal.timeout(5000) : controller?.signal
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Python connection failed.');
  return body;
}

async function selectedTab(s) {
  let tab;
  try { tab = await chrome.tabs.get(s.tabId); }
  catch (_) {
    const url=s.tabUrl && (sofaUrl(s.tabUrl)||whoUrl(s.tabUrl)||fotmobUrl(s.tabUrl)) ? s.tabUrl : 'https://www.sofascore.com/';
    tab=await chrome.tabs.create({url,active:false});s.tabId=tab.id;
    await save({tabId:tab.id});await persistConnection(s);
  }
  if (!tab.url || !(sofaUrl(tab.url) || whoUrl(tab.url) || fotmobUrl(tab.url))) throw new Error('The chosen tab left the supported website. Reconnect a Sofascore, WhoScored or FotMob tab.');
  return tab;
}

async function runPage(s, args, timeoutMs) {
  let results, timer;
  try {
    const execution = chrome.scripting.executeScript({target: {tabId: s.tabId},
      world: 'MAIN', func: pageOperation, args: [args]});
    results = timeoutMs ? await Promise.race([execution, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Page inspection timed out.')), timeoutMs);
    })]) : await execution;
  } catch (error) {
    return {ok: false, recoverable: true, error: `${error.name}: ${error.message}`};
  } finally { clearTimeout(timer); }
  const frame = results.find(r => r.frameId === 0);
  const result = frame?.result;
  if (!result || typeof result !== 'object') {
    return {ok: false, recoverable: true, error: 'Page returned no collection data.'};
  }
  result.diagnostics = {...result.diagnostics, document_id: frame.documentId};
  return result;
}

export async function navigate(s, url, forceReload = false) {
  if (!matchUrl(url)) throw new Error('Only Sofascore match pages can be opened.');
  const before = await selectedTab(s);
  const previous = sofaUrl(before.url) ? await runPage(s, {operation: 'inspect'}) : {diagnostics:{}};
  // After verification, Python may prepare the already restored match again.
  // Keep that verified document; transient page failures explicitly force reload.
  if (!forceReload && before.url === url && previous.ok &&
      !previous.diagnostics?.visible_challenge && previous.diagnostics?.match_controls &&
      matchId(previous.source_url) === matchId(url)) {
    return {...previous, diagnostics: {...previous.diagnostics, browser_load_state: before.status,
      navigation_retries: 0, navigation_reused: true}};
  }
  // A same-document hash change can select another match without replacing
  // the document. A reload/full navigation must replace the old document first.
  const oldUrl = new URL(before.url), nextUrl = new URL(url);
  const hashNavigation = oldUrl.pathname === nextUrl.pathname && oldUrl.hash !== nextUrl.hash;
  let previousDocument = previous.diagnostics?.document_id;
  let last;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt === 0) {
      if (before.url === url) await chrome.tabs.reload(s.tabId);
      else await chrome.tabs.update(s.tabId, {url});
    }
    else {
      await save({message: 'Match content not ready. Reloading this match once…'});
      await chrome.tabs.reload(s.tabId);
    }
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const tab = await selectedTab(s);
      if (matchId(tab.url) === matchId(url)) {
        try {
          last = await runPage(s, {operation: 'inspect'});
          if (last.diagnostics?.visible_challenge) return {...last, status: 403, verification: true};
          const newDocument = !previousDocument || last.diagnostics?.document_id !== previousDocument;
          if (last.ok && (newDocument || attempt === 0 && hashNavigation) &&
              last.diagnostics?.match_controls && matchId(last.source_url) === matchId(url)) {
            return {...last, diagnostics: {...last.diagnostics, browser_load_state: tab.status,
              navigation_retries: attempt}};
          }
        } catch (_) { /* Document may be changing during navigation. */ }
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    previousDocument = last?.diagnostics?.document_id || previousDocument;
  }
  return {ok: false, error: 'Match content did not become ready after one reload.',
    source_url: (await selectedTab(s)).url, diagnostics: {...last?.diagnostics, navigation_retries: 1}};
}

export async function execute(s, command) {
  if (!command || typeof command.id !== 'string' || !command.args || typeof command.args !== 'object') {
    throw new Error('Malformed collector command.');
  }
  const initialTab = await selectedTab(s);
  if (command.action === 'whoscored') return executeWho(s, command.args);
  if (command.action === 'fotmob') return executeFotmob(s, command.args);
  if (!sofaUrl(initialTab.url) && command.action !== 'navigate') {
    await chrome.tabs.update(s.tabId,{url:'https://www.sofascore.com/'});
    await waitForProvider(s,sofaUrl);
  }
  if (command.action === 'verify') {
    const mid = Number(command.args.endpoint?.match(/^event\/(\d+)/)?.[1]);
    const candidate = command.args.match_url || initialTab.url;
    const target = mid && matchUrl(candidate) && matchId(candidate) === mid ? candidate :
      mid ? `https://www.sofascore.com/event/${mid}` : null;
    const when = Date.now() + RELOAD_DELAYS[0];
    await save({verification: command, paused: true, verificationReadySince: null, verificationProbed: false,
      verificationDocument: null, verificationAwaitingDocument: false, verificationStage: null,
      verificationReloads: 0, verificationNextReloadAt: when, verificationFinalDeadline: null,
      verificationNeedsApproval: false, verificationMatchUrl: target,
      verificationRestoreKey: null, verificationRestoringMatch: false,
      message: 'Data request denied. Connected and paused; full page reload 1/3 in 1 minute.'});
    await chrome.alarms.create(RELOAD_ALARM, {when});
    await chrome.tabs.update(s.tabId, {active: true});
    await badge('!');
    await verificationDiagnostic({...s, verification: command}, 'reload_scheduled', {
      diagnostics: {reload_attempt: 1, wait_seconds: 60, next_reload_at: when}});
    void watchVerification();
    return null;
  }
  if (command.action === 'navigate') return navigate(s, command.args.url);
  if (command.action !== 'page') throw new Error('Unknown collector command.');
  let result = await runPage(s, command.args);
  if (!result.ok && result.recoverable) {
    const mid = command.args.match_id || Number(command.args.endpoint?.match(/^event\/(\d+)/)?.[1]);
    if (mid) {
      await save({message: 'Page operation failed. Reopening the intended match and retrying once…'});
      const tab = await selectedTab(s);
      const recovery = await navigate(s, matchId(tab.url) === mid && matchUrl(tab.url) ?
        tab.url : `https://www.sofascore.com/event/${mid}`, true);
      if (!recovery.ok || recovery.verification) return recovery;
      const original = result.error;
      result = await runPage(s, command.args);
      result.diagnostics = {...result.diagnostics, recovery_attempts: 1, recovery_reason: original};
    }
  }
  return result;
}

async function finishVerification(s, result, message) {
  if (resolvingVerification) return false;
  resolvingVerification = true;
  try {
    const current = await state();
    if (!current.connected || current.verification?.id !== s.verification.id) return false;
    await api(s, 'result', {id: s.verification.id, result});
    await chrome.alarms.clear(RELOAD_ALARM);
    await save({paused: false, verification: null, verificationNextReloadAt: null, message});
    await badge('ON');
    void poll();
    return true;
  } finally { resolvingVerification = false; }
}

async function verificationDiagnostic(s, stage, result = {}) {
  // The bridge filters this metadata; page contents and credentials stay out.
  try {
    await api(s, 'diagnostic', {id: s.verification.id, result: {
      endpoint: s.verification.args.endpoint, recovery_stage: stage,
      ok: result.ok, status: result.status, error: result.error,
      source_url: result.source_url, url: result.url, diagnostics: result.diagnostics}});
  } catch (_) { /* Diagnostic delivery must not interrupt browser recovery. */ }
}

async function verificationStage(s, stage, observed) {
  if (s.verificationStage === stage) return;
  await save({verificationStage: stage});
  await verificationDiagnostic(s, stage, observed);
}

async function requireApproval(s, observed = {}) {
  await save({verificationNeedsApproval: true, verificationNextReloadAt: null,
    message: 'Three full page reload attempts could not restore access. Collection is paused for your approval. Inspect Chrome, then click Resume, or Disconnect to stop.'});
  await chrome.alarms.clear(RELOAD_ALARM);
  await verificationDiagnostic(s, 'approval_required', observed);
  return false;
}

async function reloadForVerification(s) {
  const before = await runPage(s, {operation: 'inspect'}, 5000);
  const current = await state();
  if (!current.connected || current.verification?.id !== s.verification.id || current.verificationNeedsApproval) return;
  const attempt = (current.verificationReloads || 0) + 1;
  if (attempt > 3) return;
  const now = Date.now();
  const when = attempt < 3 ? now + RELOAD_DELAYS[attempt] : null;
  const deadline = attempt === 3 ? now + 60000 : null;
  // Persist the consumed attempt before reloading; worker restarts cannot loop it.
  await save({verificationReloads: attempt, verificationNextReloadAt: when,
    verificationFinalDeadline: deadline, verificationProbed: false, verificationReadySince: null,
    verificationDocument: before.diagnostics?.document_id || current.verificationDocument,
    verificationAwaitingDocument: true, verificationStage: null,
    message: `Connected and paused; performing full page reload ${attempt}/3…`});
  await chrome.alarms.create(RELOAD_ALARM, {when: when || deadline});
  const evidence = {diagnostics: {...before.diagnostics, reload_attempt: attempt,
    bypass_cache: true, next_reload_at: when}};
  await verificationDiagnostic(s, 'reload_started', evidence);
  try {
    await selectedTab(s);
    await chrome.tabs.reload(s.tabId, {bypassCache: true});
    await verificationDiagnostic(s, 'reload_requested', evidence);
  } catch (error) {
    await verificationDiagnostic(s, 'reload_failed', {...evidence, error: error.message});
    if (attempt === 3) await requireApproval(s, {error: error.message});
    else await save({message: `Reload ${attempt}/3 failed; next full reload in ${RELOAD_DELAYS[attempt]/60000} minutes. Collection remains paused.`});
  }
}

export async function checkVerification(s) {
  if (!s.connected || !s.verification || s.verificationNeedsApproval) return false;
  if (s.verificationNextReloadAt && Date.now() >= s.verificationNextReloadAt) {
    await reloadForVerification(s);
    return false;
  }
  if (s.verificationFinalDeadline && Date.now() >= s.verificationFinalDeadline) {
    return requireApproval(s);
  }
  await selectedTab(s);
  const observed = await runPage(s, {operation: 'inspect'}, 5000);
  if (!observed.ok) {
    await verificationStage(s, 'document_unavailable', observed);
    return false; // Navigation can replace a challenge document.
  }
  const doc = observed.diagnostics?.document_id;
  if (s.verificationAwaitingDocument && s.verificationDocument && doc === s.verificationDocument) {
    await verificationStage(s, 'waiting_for_reload', observed);
    return false;
  }
  // A user reload is also a new opportunity for one confirmation read.
  if (doc && doc !== s.verificationDocument) {
    const wasReload = s.verificationAwaitingDocument;
    await save({verificationDocument: doc, verificationAwaitingDocument: false,
      verificationReadySince: null, verificationProbed: false, verificationStage: null});
    s = {...s, verificationDocument: doc, verificationAwaitingDocument: false,
      verificationReadySince: null, verificationProbed: false, verificationStage: null};
    await verificationDiagnostic(s, wasReload ? 'reload_confirmed' : 'document_changed',
      {...observed, diagnostics: {...observed.diagnostics, reload_attempt: s.verificationReloads}});
  }
  // First recovery must replace the page, even if its old controls still work.
  if (s.verificationReloads === 0) return false;
  const mid = Number(s.verification.args.endpoint?.match(/^event\/(\d+)/)?.[1]);
  const observedMid = observed.source_url ? matchId(observed.source_url) : null;
  const cleared = !!observed.source_url && !observed.diagnostics?.visible_challenge &&
    ['interactive', 'complete'].includes(observed.diagnostics?.ready_state);
  if (mid && cleared && observedMid !== mid) {
    // Sofascore's verification redirect can strip #id:<match>, leaving the
    // H2H page usable but without the specific historical event selected.
    const source = new URL(observed.source_url);
    if (source.pathname === '/captcha.html') {
      await verificationStage(s, 'waiting_for_verification_return', observed);
      return false;
    }
    const key = `${doc || ''}|${observed.source_url}`;
    if (s.verificationRestoreKey !== key) {
      const target = s.verificationMatchUrl && matchUrl(s.verificationMatchUrl) &&
        matchId(s.verificationMatchUrl) === mid ? s.verificationMatchUrl :
        `https://www.sofascore.com/event/${mid}`;
      await save({verificationRestoreKey: key, verificationRestoringMatch: true,
        verificationReadySince: null, verificationProbed: false,
        message: 'Verification cleared. Restoring the selected match before retrying its data…'});
      await chrome.tabs.update(s.tabId, {url: target});
      await verificationDiagnostic(s, 'match_restore_requested', {...observed, url: target});
    }
    return false;
  }
  if (mid && observedMid === mid && s.verificationRestoringMatch) {
    await save({verificationRestoringMatch: false, verificationReadySince: null});
    s = {...s, verificationRestoringMatch: false, verificationReadySince: null};
    await verificationDiagnostic(s, 'match_restored', observed);
  }
  const ready = mid ? observed.diagnostics?.match_controls && observedMid === mid :
    ['interactive', 'complete'].includes(observed.diagnostics?.ready_state);
  if (observed.diagnostics?.visible_challenge || !ready) {
    await save({verificationReadySince: null, ...(observed.diagnostics?.visible_challenge &&
      s.verificationStage !== 'visible_challenge' ? {verificationProbed: false} : {})});
    await verificationStage(s, observed.diagnostics?.visible_challenge ? 'visible_challenge' : 'page_not_ready', observed);
    return false;
  }
  if (!s.verificationReadySince) {
    await save({verificationReadySince: Date.now()});
    await verificationStage(s, 'settling', observed);
    return false;
  }
  if (Date.now() - s.verificationReadySince < 4000 || s.verificationProbed) return false;
  // Claim the one confirmation read before awaiting network work.
  await save({verificationProbed: true});
  const result = await runPage(s, {operation: 'read', endpoint: s.verification.args.endpoint,
    timeout_ms: 15000}, 20000);
  const current = await state();
  if (!current.connected || current.verification?.id !== s.verification.id) return false;
  await verificationDiagnostic(s, 'confirmation_read', result);
  if (!result.ok || ![200, 404, 429].includes(result.status)) {
    if (s.verificationReloads >= 3) return requireApproval(s, result);
    await save({message: `Access is still denied after reload ${s.verificationReloads || 1}/3. Collection is waiting, not stopped; next reload is scheduled. You can also inspect Chrome and click Resume.`});
    return false;
  }
  return finishVerification(s, {ok: true, status: result.status,
    retryAfter: result.retryAfter, source_url: result.source_url,
    diagnostics: {...result.diagnostics, verification_resolution: 'automatic'}},
    'Browser access restored; collection resumed.');
}

async function watchVerification() {
  if (verifying) return;
  verifying = true;
  try {
    while (true) {
      const s = await state();
      if (!s.connected || !s.verification || s.verificationNeedsApproval) break;
      if (await checkVerification(s)) break;
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  } catch (error) {
    await save({message: error.message + ' Collection remains paused; reconnect if the chosen tab was closed.'});
  } finally { verifying = false; }
}

async function poll() {
  if (polling) return;
  polling = true;
  controller = new AbortController();
  try {
    while (true) {
      const s = await state();
      if (!s.connected || s.paused) break;
      if (s.reconnecting) {
        await selectedTab(s);
        await api(s,'connect',{client_id:s.clientId});
        await save({reconnecting:false,message:'Reconnected automatically.'});
        await badge('ON');
      }
      const body = await api(s, 'command');
      if (body.phase === 'closed') {
        await save({connected:true,reconnecting:true,message:'Python stopped or is restarting. Waiting to reconnect automatically.'});
        await badge('…');
        break;
      }
      if (!body.command) continue;
      await save({message: body.command.action === 'navigate' ? 'Opening match…' : 'Collecting match data…'});
      let result;
      try { result = await execute(s, body.command); }
      catch (error) { result = {ok: false, error: error.message}; }
      if (result === null) break;
      await api(s, 'result', {id: body.command.id, result});
    }
  } catch (error) {
    if (error.name !== 'AbortError') {
      await save({connected:true,reconnecting:true,message:'Python connection interrupted. Retrying automatically; saved work is safe.'});
      await badge('!');
    }
  } finally {
    polling = false;
    const s = await state();
    if (s.connected && !s.paused) {const timer=setTimeout(() => void poll(), s.reconnecting ? 5000 : 0);timer?.unref?.();}
  }
}

async function message(msg, sender) {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('popup.html')) {
    throw new Error('Only the extension popup can control collection.');
  }
  if (msg.action === 'status') return state();
  if (msg.action === 'connect') {
    const old = await state();
    if (old.connected) throw new Error('Disconnect before choosing another tab.');
    const connection = connectionCode(msg.code);
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.url || !(sofaUrl(tab.url) || whoUrl(tab.url) || fotmobUrl(tab.url))) throw new Error('Open Sofascore, WhoScored or FotMob in this tab before connecting.');
    const same = old.base === connection.base && old.token === connection.token;
    const s = {...connection, tabId: tab.id, tabUrl:tab.url, clientId: same && old.clientId || crypto.randomUUID().replaceAll('-', '')};
    controller?.abort();
    controller = new AbortController();
    await api(s, 'connect', {client_id: s.clientId});
    await chrome.storage.session.clear();
    await save({...s, connected: true, paused: false, verification: null, message: 'Connected. Python controls only this selected tab.'});
    await persistConnection(s);
    await chrome.alarms.create('collector', {periodInMinutes: 0.5});
    await badge('ON');
    void poll();
  } else if (msg.action === 'pause') {
    const s = await state();
    if (!s.connected) throw new Error('Connect to Python first.');
    await api(s, 'pause', {});
    await save({paused: true, message: 'Paused after the current step. Click Resume when ready.'});
    await persistConnection({...s,paused:true});
    await badge('II');
  } else if (msg.action === 'resume') {
    const s = await state();
    if (!s.connected) throw new Error('Connect to the running Python collector first.');
    await selectedTab(s);
    if(s.reconnecting){await api(s,'connect',{client_id:s.clientId});await save({reconnecting:false});}
    await api(s, 'resume', {});
    if (s.verification) {
      await finishVerification(s, {ok: true, diagnostics: {verification_resolution: 'manual'}}, 'Resumed.');
      return {ok: true};
    }
    await save({paused: false, verification: null, message: 'Resumed.'});
    await persistConnection({...s,paused:false});
    await badge('ON');
    void poll();
  } else if (msg.action === 'disconnect') {
    const s = await state();
    controller?.abort();
    controller = undefined;
    if (s.clientId) {
      try { await api(s, 'disconnect', {}); } catch (_) { /* Python may already be stopped. */ }
    }
    await chrome.storage.session.clear();
    if (chrome.storage.local) await chrome.storage.local.remove('collectorConnection');
    await chrome.alarms.clear('collector');
    await chrome.alarms.clear(RELOAD_ALARM);
    await save({connected: false, message: 'Disconnected.'});
    await badge('');
  } else throw new Error('Unknown popup action.');
  return {ok: true};
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  message(msg, sender).then(reply).catch(async error => {
    await save({message: error.message});
    reply({ok: false, error: error.message});
  });
  return true;
});
async function resumeSavedConnection(){
  const s=await state();if(s.connected){await save({reconnecting:true});await chrome.alarms.create('collector',{periodInMinutes:0.5});void poll();}
}
if(chrome.runtime.onStartup) chrome.runtime.onStartup.addListener(resumeSavedConnection);
if(chrome.runtime.onInstalled) chrome.runtime.onInstalled.addListener(resumeSavedConnection);
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name === 'collector' || alarm.name === RELOAD_ALARM) {
    const s = await state();
    if (s.verification) void watchVerification();
    else void poll();
  }
});

export async function executeWho(s,args) {
  const initial=await selectedTab(s);
  if (!whoEndpoint(args.endpoint,args.kind,args.params)) throw new Error('Unknown WhoScored collection endpoint.');
  const stage=args.kind==='json' ? args.endpoint.match(/^tournaments\/(\d+)/)[1] : null;
  const url='https://www.whoscored.com/'+(stage ? `stages/${stage}/fixtures` : args.endpoint);
  if (initial.url.replace(/\/$/,'') !== url) await chrome.tabs.update(s.tabId,{url});
  const dataWait=Math.min(60000,Math.max(1000,args.timeout_ms || 30000));
  const manualWait=Math.min(300000,Math.max(1000,args.manual_wait_ms || 120000));
  let deadline=Date.now()+dataWait;
  let result, waitingSince=null;
  while (Date.now()<deadline) {
    const current=await selectedTab(s);
    if (!whoUrl(current.url)) {await new Promise(r=>setTimeout(r,500));continue;}
    // Wait for the intended document; never read the previous game's source.
    const currentPath=new URL(current.url).pathname.toLowerCase().replace(/\/$/,'');
    const wantedPath=new URL(url).pathname.toLowerCase().replace(/\/$/,'');
    const expectedPlayer=args.endpoint.match(/^players\/(\d+)\/history$/i)?.[1];
    const playerPath=expectedPlayer && new RegExp(`^/players/${expectedPlayer}/history(?:/|$)`,'i').test(currentPath);
    const expectedMatch=args.endpoint.match(/^matches\/(\d+)\/live$/i)?.[1];
    const correctPath=expectedPlayer ? playerPath : expectedMatch ? new RegExp(`^/matches/${expectedMatch}/live(?:/|$)`).test(currentPath) : currentPath===wantedPath;
    if (args.kind==='text' && !correctPath) {
      await new Promise(r=>setTimeout(r,500));continue;
    }
    const frames=await chrome.scripting.executeScript({target:{tabId:s.tabId},world:'MAIN',func:whoPageOperation,args:[args]});
    result=frames.find(f=>f.frameId===0)?.result;
    if (result?.status===403 && result.verification) {
      if (waitingSince===null) {waitingSince=Date.now();deadline=Math.max(deadline,waitingSince+manualWait);}
      await save({message:'WhoScored verification: finish the check in the selected tab. Collection is waiting.'});
      await new Promise(r=>setTimeout(r,1000));continue;
    }
    if (result?.status===200 && args.kind==='text' && /^matches\//.test(args.endpoint) &&
        (!/\bmatchCentreData["']?\s*[:=]\s*\{/.test(result.text || '') ||
         result.text.match(/\bmatchId["']?\s*[:=]\s*(\d+)/)?.[1]!==expectedMatch)) {await new Promise(r=>setTimeout(r,500));continue;}
    if (result?.status===200 && expectedPlayer && (!/player-table-statistics-body[\s\S]*?<tr\b/i.test(result.text || '') || !/player-picture/.test(result.text || ''))) {await new Promise(r=>setTimeout(r,500));continue;}
    if (result) return {...result,waiting_seconds:waitingSince===null ? 0 : (Date.now()-waitingSince)/1000};
    await new Promise(r=>setTimeout(r,500));
  }
  if (result?.status===403 && result.verification) return {...result,reason:'verification_required',
    waiting_seconds:waitingSince===null ? 0 : (Date.now()-waitingSince)/1000};
  return {ok:true,status:null,reason:'extension_timeout',error:'WhoScored data did not become ready before the extension deadline.',
    waiting_seconds:waitingSince===null ? 0 : (Date.now()-waitingSince)/1000,source_url:(await selectedTab(s)).url};
}

async function waitForProvider(s,accept) {
  const deadline=Date.now()+30000;
  while (Date.now()<deadline) {
    const tab=await selectedTab(s);
    if (accept(tab.url)) {
      if (tab.status==='complete') return tab;
      try {
        const frames=await chrome.scripting.executeScript({target:{tabId:s.tabId},world:'MAIN',func:()=>document.readyState});
        if (frames.some(f=>f.frameId===0 && ['interactive','complete'].includes(f.result))) return tab;
      } catch (_) { /* The previous document may be unloading. */ }
    }
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw new Error('Provider page did not finish opening; reconnect and resume.');
}
export async function executeFotmob(s,args) {
  const initial=await selectedTab(s);
  if (!['leagues','matchDetails','matches','playerData'].includes(args.endpoint)) throw new Error('Unknown FotMob endpoint.');
  if (!fotmobUrl(initial.url)) await chrome.tabs.update(s.tabId,{url:'https://www.fotmob.com/'});
  await waitForProvider(s,fotmobUrl);
  const frames=await chrome.scripting.executeScript({target:{tabId:s.tabId},world:'MAIN',func:fotmobPageOperation,args:[args]});
  return frames.find(f=>f.frameId===0)?.result || {ok:false,error:'FotMob returned no page data.'};
}
