// Self-contained: Chrome serializes this function into the chosen page.
// No connection token, account credentials or extension APIs enter the page.
export async function pageOperation(args) {
  const origin = 'https://www.sofascore.com';
  if (location.origin !== origin) throw new Error('The selected tab left Sofascore.');
  const challenge = /verif(?:y|ying) you are (?:a )?human|checking your browser|security verification|access denied/i;
  const source = () => ({source_url: location.href});
  const challenged = () => challenge.test(document.body?.innerText || '');
  const verification = () => ({ok: true, status: 403, verification: true,
    challenge_evidence: 'visible_page', diagnostics: observe(), ...source()});
  const visible = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const observe = () => ({
    title: (document.title || '').slice(0, 160), ready_state: document.readyState,
    visible_challenge: challenged(),
    match_controls: !!document.querySelector('[data-testid="tab-lineups"], [data-testid="tab-statistics"], [data-testid="tab-details"]'),
    player_panels: [...document.querySelectorAll('[data-testid="sheet"]')].filter(visible).length,
    dialogs: [...document.querySelectorAll('[role="dialog"]')].filter(visible).slice(0, 5)
      .map(el => ({label: (el.getAttribute('aria-label') || '').slice(0, 100),
        players: el.querySelectorAll('img[src*="/player/"]').length}))
  });
  if (args.operation === 'inspect') return {ok: true, ...source(), diagnostics: observe()};
  if (challenged()) return verification();
  if (args.operation === 'read') {
    const allowed = /^(?:event\/\d+(?:\/(?:lineups|statistics|incidents|graph\/momentum|average-positions|shotmap|player\/\d+\/(?:statistics|heatmap|rating-breakdown)))?|unique-tournament\/\d+\/(?:seasons|season\/\d+\/events\/(?:last|next)\/\d+))$/;
    if (typeof args.endpoint !== 'string' || !allowed.test(args.endpoint) ||
        (args.params != null && (typeof args.params !== 'object' || Object.keys(args.params).length))) {
      throw new Error('Resource is outside the collection contract.');
    }
    if (!Number.isFinite(args.timeout_ms) || args.timeout_ms <= 0 || args.timeout_ms > 300000) {
      throw new Error('Invalid resource timeout.');
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), args.timeout_ms);
    try {
      const response = await fetch('/api/v1/' + args.endpoint,
        {credentials: 'same-origin', signal: controller.signal});
      const text = await response.text();
      let payload = null;
      try { payload = JSON.parse(text); } catch (_) { /* A challenge can be HTML. */ }
      if (!payload && /cf-chl-|challenges\.cloudflare\.com|verify you are/i.test(text)) {
        return {...verification(), challenge_evidence: 'response_html', diagnostics: observe()};
      }
      return {ok: true, status: response.status, payload,
        verification: false, retryAfter: response.headers.get('Retry-After'), ...source(),
        diagnostics: {...observe(), content_type: response.headers.get('Content-Type')}};
    } catch (error) {
      return {ok: false, error: `${error.name}: ${error.message}`, recoverable: true,
        ...source(), diagnostics: observe()};
    } finally { clearTimeout(timer); }
  }
  if (args.operation !== 'prepare' || !['lineups', 'statistics', 'details'].includes(args.tab)) {
    throw new Error('Unknown page operation.');
  }
  const current = location.hash.match(/(?:^#|,)id:(\d+)/) || location.pathname.match(/^\/event\/(\d+)$/);
  if (!Number.isSafeInteger(args.match_id) || args.match_id < 1) throw new Error('Invalid match ID.');
  if (current && Number(current[1]) !== args.match_id) return {ok: true, status: 409, ...source()};
  const lo = args.delay_min_seconds, hi = args.delay_max_seconds;
  if (![lo, hi].every(Number.isFinite) || lo < 0 || hi < lo || hi > 60) {
    throw new Error('Extension click spacing must be between 0 and 60 seconds.');
  }
  if (args.player_id != null && (!Number.isSafeInteger(args.player_id) || args.player_id < 1)) {
    throw new Error('Invalid player ID.');
  }
  const actions = [];
  const pause = async () => {
    const milliseconds = (lo + Math.random() * (hi - lo)) * 1000;
    actions.push({action: 'wait', milliseconds: Math.round(milliseconds)});
    await new Promise(resolve => setTimeout(resolve, milliseconds));
  };
  const click = async el => {
    if (!visible(el)) return false;
    await pause();
    if (challenged()) return false;
    el.click();
    actions.push({action: 'click', label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 100)});
    await new Promise(resolve => setTimeout(resolve, 250));
    return true;
  };
  const buttons = () => [...document.querySelectorAll('button')];
  const labels = new Set(['CONTINUE WITH LIMITED ACCESS', 'Reject all', 'Reject All',
    'Close ad', 'Close advertisement', 'Dismiss ad', 'Skip ad', 'Close slide-in banner', 'Dismiss banner']);
  for (const button of buttons()) {
    const label = button.getAttribute('aria-label') || button.innerText.trim();
    if (labels.has(label)) await click(button);
  }
  // Only close an explicitly identified player panel, not a generic page control.
  const sheet = [...document.querySelectorAll('[data-testid="sheet"]')].find(visible);
  if (sheet) {
    const close = [...sheet.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Close');
    if (close) await click(close);
  }
  const tab = document.querySelector(`[data-testid="tab-${args.tab}"]`);
  const tabOpened = await click(tab);
  let playerOpened = false;
  let playerTarget = 'not_requested';
  if (args.player_id != null) {
    const images = [...document.querySelectorAll(`img[src*="/player/${args.player_id}/image"]`)];
    const image = images.find(visible);
    // Click the image itself so its nearest site handler receives the event.
    // A fixed grandparent can instead be a substitution/player chooser.
    playerTarget = image ? 'image' : 'not_visible';
    if (image) playerOpened = await click(image);
  }
  if (challenged()) return verification();
  return {ok: true, tab_opened: tabOpened, player_opened: playerOpened, ...source(),
    diagnostics: {...observe(), player_target: playerTarget, actions}};
}
