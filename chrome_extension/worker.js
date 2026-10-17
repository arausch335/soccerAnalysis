import {connectionCode, sofaUrl, matchUrl, matchId} from './protocol.js';
import {pageOperation} from './page.js';

let polling = false;
let controller;
const state = () => chrome.storage.session.get(null);
const save = values => chrome.storage.session.set(values);
const badge = text => chrome.action.setBadgeText({text});

async function api(s, path, value) {
  const response = await fetch(s.base + '/v1/' + path, {
    method: value === undefined ? 'GET' : 'POST',
    headers: {Authorization: 'Bearer ' + s.token, 'X-Collector-Client': s.clientId,
      ...(value === undefined ? {} : {'Content-Type': 'application/json'})},
    body: value === undefined ? undefined : JSON.stringify(value),
    signal: controller?.signal
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Python connection failed.');
  return body;
}

async function selectedTab(s) {
  const tab = await chrome.tabs.get(s.tabId);
  if (!tab.url || !sofaUrl(tab.url)) throw new Error('The chosen tab left Sofascore. Reconnect a Sofascore tab.');
  return tab;
}

async function navigate(s, url) {
  if (!matchUrl(url)) throw new Error('Only Sofascore match pages can be opened.');
  await selectedTab(s);
  await chrome.tabs.update(s.tabId, {url});
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const tab = await selectedTab(s);
    if (tab.status === 'complete' && matchId(tab.url) === matchId(url)) return {ok: true, source_url: tab.url};
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Match page did not finish loading.');
}

async function execute(s, command) {
  if (!command || typeof command.id !== 'string' || !command.args || typeof command.args !== 'object') {
    throw new Error('Malformed collector command.');
  }
  await selectedTab(s);
  if (command.action === 'verify') {
    await save({verification: command, paused: true,
      message: 'Complete verification yourself in the chosen Chrome tab, then click Resume.'});
    await chrome.tabs.update(s.tabId, {active: true});
    await badge('!');
    return null;
  }
  if (command.action === 'navigate') return navigate(s, command.args.url);
  if (command.action !== 'page') throw new Error('Unknown collector command.');
  const results = await chrome.scripting.executeScript({target: {tabId: s.tabId},
    world: 'MAIN', func: pageOperation, args: [command.args]});
  const result = results.find(r => r.frameId === 0)?.result;
  if (!result || typeof result !== 'object') throw new Error('Page returned no collection data.');
  return result;
}

async function poll() {
  if (polling) return;
  polling = true;
  controller = new AbortController();
  try {
    while (true) {
      const s = await state();
      if (!s.connected || s.paused) break;
      const body = await api(s, 'command');
      if (body.phase === 'closed') {
        await save({connected: false, message: 'Python collection finished or stopped. Saved checkpoints remain available.'});
        await badge('');
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
      await save({connected: false, message: error.message + ' Saved Python checkpoints remain available.'});
      await badge('!');
    }
  } finally {
    polling = false;
    const s = await state();
    if (s.connected && !s.paused) setTimeout(() => void poll(), 0);
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
    if (!tab?.url || !sofaUrl(tab.url)) throw new Error('Open Sofascore in this tab before connecting.');
    const same = old.base === connection.base && old.token === connection.token;
    const s = {...connection, tabId: tab.id, clientId: same && old.clientId || crypto.randomUUID().replaceAll('-', '')};
    controller?.abort();
    controller = new AbortController();
    await api(s, 'connect', {client_id: s.clientId});
    await chrome.storage.session.clear();
    await save({...s, connected: true, paused: false, verification: null, message: 'Connected. Python controls only this Sofascore tab.'});
    await chrome.alarms.create('collector', {periodInMinutes: 0.5});
    await badge('ON');
    void poll();
  } else if (msg.action === 'pause') {
    const s = await state();
    if (!s.connected) throw new Error('Connect to Python first.');
    await api(s, 'pause', {});
    await save({paused: true, message: 'Paused after the current step. Click Resume when ready.'});
    await badge('II');
  } else if (msg.action === 'resume') {
    const s = await state();
    if (!s.connected) throw new Error('Connect to the running Python collector first.');
    await selectedTab(s);
    await api(s, 'resume', {});
    if (s.verification) {
      await api(s, 'result', {id: s.verification.id, result: {ok: true}});
    }
    await save({paused: false, verification: null, message: 'Resumed.'});
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
    await chrome.alarms.clear('collector');
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
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'collector') void poll(); });
