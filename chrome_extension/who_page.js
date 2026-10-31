// Runs only in the selected WhoScored document. Returns source data, never cookies.
export async function whoPageOperation(args) {
  if (location.origin !== 'https://www.whoscored.com') throw new Error('Choose a WhoScored tab.');
  const base = {ok:true, source_url:location.href};
  const challenge = () => /verify you are (?:a )?human|access denied|checking your browser|just a moment|incapsula incident|pardon our interruption/i.test(document.body?.innerText || '') ||
    !!document.querySelector('iframe[src*="captcha"], iframe[src*="challenges.cloudflare.com"], #challenge-running');
  if (challenge()) return {...base,status:403,verification:true};
  if (args.kind === 'text') {
    const text=document.documentElement.outerHTML;
    if (/^regions\//i.test(args.endpoint || '')) {
      const seasons=document.querySelector('select#seasons');
      const context=(args.endpoint || '').match(/^regions\/\d+\/tournaments\/\d+\/seasons\/\d+(?=\/|$)/i)?.[0];
      const fixtures=context && new RegExp(`(?:href|value)=["']/?${context}/stages/\\d+/(?:show|fixtures)/`,'i').test(text);
      if (document.readyState!=='complete' || !seasons?.options?.length || (context && !fixtures)) {
        return {...base,status:202,reason:'directory_loading'};
      }
    }
    const match=(args.endpoint || '').match(/^matches\/(\d+)\/live$/i);
    const sameMatch=match && text.match(/\bmatchId["']?\s*[:=]\s*(\d+)/)?.[1]===match[1];
    const hasData=/\bmatchCentreData["']?\s*[:=]\s*\{/.test(text);
    const visible=(document.body?.innerText || '').replace(/\s+/g,' ');
    const missing=/\bno (?:match )?(?:statistics|stats|data)(?: (?:is|are))? available\b|\b(?:match )?(?:statistics|stats|data) (?:is|are) (?:not available|unavailable)\b/i.test(visible);
    if (sameMatch && document.readyState==='complete' && !hasData && missing) {
      return {...base,status:404,reason:'match_data_unavailable',match_id:match[1]};
    }
    return {...base,status:200,text};
  }
  if (args.kind !== 'json' || !/^tournaments\/\d+\/data\/$/.test(args.endpoint) ||
      !args.params || Object.keys(args.params).sort().join(',') !== 'd,isAggregate' ||
      !/^\d{6}$/.test(args.params.d) || args.params.isAggregate !== 'false') {
    throw new Error('Unknown WhoScored fixture request.');
  }
  const stage=args.endpoint.match(/^tournaments\/(\d+)/)[1];
  if (!new RegExp(`(?:^|/)stages/${stage}/(?:show|fixtures)(?:/|$)`, 'i').test(location.pathname)) throw new Error('Wrong WhoScored fixture stage.');
  const response=await fetch('/'+args.endpoint+'?'+new URLSearchParams(args.params), {
    credentials:'same-origin',signal:AbortSignal.timeout(Math.min(60000,Math.max(1000,args.timeout_ms || 30000))),
    headers:{'X-Requested-With':'XMLHttpRequest','Accept':'application/json'}
  });
  const text=await response.text();
  if (/cf-chl-|captcha|incapsula incident|access denied|<html/i.test(text)) return {...base,status:403};
  if (response.status !== 200) return {...base,status:response.status,retryAfter:response.headers.get('Retry-After')};
  try {return {...base,status:200,payload:JSON.parse(text)};}
  catch (_) {return {...base,status:502};}
}
