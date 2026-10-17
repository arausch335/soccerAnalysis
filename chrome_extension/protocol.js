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
