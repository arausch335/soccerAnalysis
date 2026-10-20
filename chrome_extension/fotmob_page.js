/** Read only the supported FotMob JSON endpoints in the chosen tab's origin. */
export async function fotmobPageOperation(args) {
  const source_url=location.href;
  if (location.protocol!=='https:' || location.hostname!=='www.fotmob.com') return {ok:false,error:'Open FotMob in the selected tab.'};
  const rules={playerData:{allowed:['id'],required:['id']},leagues:{allowed:['id','ccode3','season'],required:['id']},matchDetails:{allowed:['matchId'],required:['matchId']},matches:{allowed:['date'],required:['date']}};
  const rule=rules[args.endpoint],params=args.params || {};
  if (!rule || Object.keys(params).some(k=>!rule.allowed.includes(k)) || rule.required.some(k=>!(k in params))) return {ok:false,error:'Unsupported FotMob request.'};
  if (('matchId' in params && !/^\d+$/.test(String(params.matchId))) || ('id' in params && !/^\d+$/.test(String(params.id))) || ('date' in params && !/^\d{8}$/.test(params.date))) return {ok:false,error:'Invalid FotMob identifier.'};
  const timer=new AbortController();const timeout=setTimeout(()=>timer.abort(),Math.min(60000,args.timeout_ms || 30000));
  try {
    const response=await fetch('/api/data/'+args.endpoint+'?'+new URLSearchParams(params),{credentials:'same-origin',signal:timer.signal});
    const text=await response.text();
    let payload;try {payload=JSON.parse(text);} catch (_) {return {ok:true,status:response.status===200?403:response.status,verification:true,source_url};}
    return {ok:true,status:response.status,payload,source_url,retryAfter:response.headers.get('Retry-After')};
  } catch (error) {return {ok:false,error:error.message,source_url};}
  finally {clearTimeout(timeout);}
}
