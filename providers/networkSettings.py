"""Coherent HTTP identities and optional per-session proxy routing."""
import json
from http.cookiejar import MozillaCookieJar
import os
from pathlib import Path
import random
import re
from urllib.parse import urlsplit

from curl_cffi.requests import Session
from curl_cffi.requests.impersonate import BrowserType
from curl_cffi.requests import impersonate as _profiles
from storage.pandasStore import atomic_json
from .httpClient import ProviderError


def proxy_options(env_name):
    """Resolve only the explicitly configured environment variable; never log it."""
    value=os.environ.get(env_name,'') if env_name else ''
    if not value:return None
    parsed=urlsplit(value)
    if parsed.scheme not in ('http','https','socks5','socks5h') or not parsed.hostname:
        raise ProviderError('Proxy environment variable must contain an HTTP(S) or SOCKS5 URL')
    return value


def redact(message):
    return re.sub(r'(?i)(https?|socks5h?)://[^\s/@]+:[^\s/@]+@',r'\1://[redacted]@',str(message))


def make_session(root,provider,referer,settings):
    supported={item.value for item in BrowserType}|{'chrome','safari','firefox','edge'}
    profiles=settings['profiles']
    if set(profiles)-supported:
        raise ProviderError('Unsupported curl_cffi browser profile; update the profile list or curl_cffi')
    path=Path(root)/'http_sessions'/provider/'identity.json'
    identity=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    changed=(identity.get('profile') not in profiles or identity.get('language') not in settings['languages'])
    if changed:
        identity={'profile':random.SystemRandom().choice(profiles),
                  'language':random.SystemRandom().choice(settings['languages'])}
        identity['resolved_profile']=getattr(_profiles,'REAL_TARGET_MAP',{}).get(identity['profile'],identity['profile'])
        atomic_json(path,identity)
    cookie_path=path.with_name('cookies.txt')
    jar=MozillaCookieJar(str(cookie_path))
    if cookie_path.exists() and not changed:
        try:jar.load(ignore_discard=True,ignore_expires=False)
        except (OSError,ValueError) as exc:raise ProviderError('Invalid saved HTTP cookie jar') from exc
    proxy=proxy_options(settings['proxy_env'])
    # curl_cffi supplies the matching User-Agent, client hints, TLS and HTTP
    # profile. Override only the locale and endpoint-specific accept/referer.
    session=Session(impersonate=identity.get('resolved_profile',identity['profile']),trust_env=False,cookies=jar,
        headers={'Accept':'application/json, text/plain, */*',
                 'Referer':referer,'Accept-Language':identity['language']},
        **({'proxy':proxy} if proxy else {}))
    session._soccer_cookie_path=cookie_path
    return session,dict(identity,proxy_configured=bool(proxy))


def save_cookies(session):
    path=getattr(session,'_soccer_cookie_path',None)
    if path is None:return
    from uuid import uuid4
    temporary=Path(path).with_name('cookies.'+uuid4().hex+'.tmp')
    try:
        session.cookies.jar.save(str(temporary),ignore_discard=True,ignore_expires=False)
        temporary.chmod(0o600)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
