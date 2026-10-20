export function connectionCode(text) {
  const url = new URL(text.trim());
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
      url.username || url.password || url.pathname !== '/' || url.search ||
      !/^[A-Za-z0-9_-]{40,80}$/.test(url.hash.slice(1))) {
    throw new Error('Paste the complete connection code printed by main.py.');
  }
  return {base: url.origin, token: url.hash.slice(1)};
}

export function sofaUrl(text) {
  const url = new URL(text);
  return url.protocol === 'https:' && url.hostname === 'www.sofascore.com' &&
    !url.username && !url.password && !url.port;
}

export function matchUrl(text) {
  if (!sofaUrl(text)) return false;
  const url = new URL(text);
  return !url.search && (/^\/event\/\d+$/.test(url.pathname) ||
    /^\/football\/match\/[a-z0-9-]+\/[A-Za-z0-9]+\/?$/.test(url.pathname));
}

export function matchId(text) {
  const url = new URL(text);
  const found = url.hash.match(/(?:^#|,)id:(\d+)/) || url.pathname.match(/^\/event\/(\d+)$/);
  return found ? Number(found[1]) : null;
}

export function whoUrl(text) {
  try {
    const url=new URL(text);
    return url.protocol==='https:' && url.hostname==='www.whoscored.com' &&
      !url.username && !url.password && !url.port;
  } catch (_) {return false;}
}

export function whoEndpoint(endpoint,kind,params={}) {
  if (typeof endpoint!=='string' || endpoint.includes('..') || /[?#\\]/.test(endpoint)) return false;
  if (kind==='json') return /^tournaments\/\d+\/data\/$/.test(endpoint) &&
    Object.keys(params).sort().join(',')==='d,isAggregate' && /^\d{6}$/.test(params.d) && params.isAggregate==='false';
  return kind==='text' && Object.keys(params).length===0 &&
    /^(?:players\/\d+\/history|matches\/\d+\/live|regions\/\d+\/tournaments\/\d+\/[a-z0-9-]+|regions\/\d+\/tournaments\/\d+\/seasons\/\d+(?:\/(?:stages\/\d+\/(?:show|fixtures)\/)?[a-z0-9-]+)?|seasons\/\d+\/[a-z0-9-]+|stages\/\d+\/[a-z0-9-]+)$/i.test(endpoint);
}

export function fotmobUrl(text) {
  try {const u=new URL(text);return u.protocol==='https:' && u.hostname==='www.fotmob.com' && !u.username && !u.password && !u.port;} catch (_) {return false;}
}
