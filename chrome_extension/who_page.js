// Runs only in the selected WhoScored document. Returns source data, never cookies.
export async function whoPageOperation(args) {
  if (location.origin !== 'https://www.whoscored.com') throw new Error('Choose a WhoScored tab.');
  const base = {ok:true, source_url:location.href};
  const challenge = () => /verify you are (?:a )?human|access denied|checking your browser|just a moment|incapsula incident|pardon our interruption/i.test(document.body?.innerText || '') ||
    !!document.querySelector('iframe[src*="captcha"], iframe[src*="challenges.cloudflare.com"], #challenge-running');
  if (challenge()) return {...base,status:403,verification:true};
  if (args.kind === 'text') return {...base,status:200,text:document.documentElement.outerHTML};
  if (args.kind !== 'json' || !/^tournaments\/\d+\/data\/$/.test(args.endpoint) ||
      !args.params || Object.keys(args.params).sort().join(',') !== 'd,isAggregate' ||
      !/^\d{6}$/.test(args.params.d) || args.params.isAggregate !== 'false') {
    throw new Error('Unknown WhoScored fixture request.');
  }
  const stage=args.endpoint.match(/^tournaments\/(\d+)/)[1];
  if (!new RegExp(`^/stages/${stage}/`, 'i').test(location.pathname)) throw new Error('Wrong WhoScored fixture stage.');
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
