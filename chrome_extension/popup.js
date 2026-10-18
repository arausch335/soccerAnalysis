const $ = id => document.getElementById(id);
$('version').textContent = 'Version ' + chrome.runtime.getManifest().version;
async function refresh() {
  const s = await chrome.runtime.sendMessage({action: 'status'});
  if (s.error) throw new Error(s.error);
  const countdown = s.verificationNextReloadAt && s.verification && !s.verificationNeedsApproval ?
    ` Next reload in ${Math.max(0, Math.ceil((s.verificationNextReloadAt - Date.now())/1000))} seconds.` : '';
  $('status').textContent = (s.message || 'Not connected.') + countdown;
  $('connect').disabled = !!s.connected;
  $('pause').disabled = !s.connected || s.paused;
  $('resume').disabled = !s.connected || !s.paused;
  $('disconnect').disabled = !s.clientId;
}
for (const action of ['connect', 'pause', 'resume', 'disconnect']) {
  $(action).addEventListener('click', async () => {
    try {
      const result = await chrome.runtime.sendMessage({action, code: $('code').value});
      if (!result.ok) throw new Error(result.error);
      if (action === 'connect' || action === 'disconnect') $('code').value = '';
      await refresh();
    } catch (error) { $('status').textContent = error.message; }
  });
}
await refresh();
setInterval(() => refresh().catch(error => { $('status').textContent = error.message; }), 1000);
