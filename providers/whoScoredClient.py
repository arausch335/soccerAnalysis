"""WhoScored structured page data, decoded as JSON without executing JavaScript."""
import ast
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import unquote, quote
from .httpClient import HttpClient, ProviderError


def directory_path(path):
    """Validate published routes before decoding UTF-8 slugs (never URL syntax)."""
    if not isinstance(path, str):
        raise ValueError('Invalid WhoScored directory path')
    if re.search(r'%(?:2f|5c|2e)',path,re.I):raise ValueError('Encoded URL syntax in WhoScored route')
    path = unquote(unescape(path), errors='strict')
    if path.startswith('//'):raise ValueError('Invalid WhoScored absolute route')
    path = path.lstrip('/')
    if not re.fullmatch(r'regions/\d+/tournaments/\d+/(?:seasons/\d+(?:/(?:stages/\d+/(?:show|fixtures)/)?[^/]+)?|(?:show/)?[^/]+)/?', path, re.I):
        raise ValueError('Invalid WhoScored directory route')
    slug = path.rstrip('/').rsplit('/', 1)[-1]
    if not slug or any(not (c.isalnum() or c == '-') for c in slug):
        raise ValueError('Invalid WhoScored directory slug')
    return quote(path.rstrip('/'), safe='/-').lower()


class _Directory(HTMLParser):
    def __init__(self):
        super().__init__(); self.select=None; self.option=None; self.options=[];self.stage_paths=[];self.links=[];self.competition_paths=[];self.stage_names={};self.stage_option=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='select': self.select=attrs.get('id')
        if tag=='option' and self.select=='seasons': self.option=[attrs.get('value'), '']
        if tag=='option' and self.select=='stages' and attrs.get('value'):
            self.stage_paths.append(attrs['value']);self.stage_option=[attrs['value'], '']
        if tag=='option' and self.select=='tournaments' and attrs.get('value'):
            self.competition_paths.append(attrs['value'])
        if tag=='a' and attrs.get('href'): self.links.append(attrs['href'])

    def handle_data(self, data):
        if self.option is not None: self.option[1]+=data
        if self.stage_option is not None:self.stage_option[1]+=data

    def handle_endtag(self, tag):
        if tag=='option' and self.option is not None: self.options.append(self.option); self.option=None
        if tag=='option' and self.stage_option is not None:
            self.stage_names[self.stage_option[0]]=self.stage_option[1].strip();self.stage_option=None
        if tag=='select': self.select=None


def published_stages(parser,season_path):
    """Return only published stage routes belonging to the requested season."""
    context=re.search(r'(regions/\d+/tournaments/\d+/seasons/\d+)',season_path,re.I)
    paths={}
    for raw in (*parser.stage_paths,*parser.links):
        try:path=directory_path(raw)
        except (ValueError,UnicodeError):continue
        if context and not path.startswith(context[1].lower()+'/'):continue
        found=re.search(r'/stages/(\d+)/(show|fixtures)/',path,re.I)
        if found:
            stage=int(found[1])
            if stage not in paths or found[2].lower()=='fixtures':paths[stage]=path
    return paths


def embedded_json(text, key):
    # The provider's object values are JSON; the enclosing assignment is JS.
    found=list(re.finditer(r'\b'+re.escape(key)+r'[\"\']?\s*[:=]\s*',text))
    if not found:
        raise ProviderError(f'WhoScored page is missing {key}; it may be blocked or its format changed')
    objects=[]
    for occurrence in found:
        try:value,_=json.JSONDecoder().raw_decode(text[occurrence.end():])
        except ValueError:continue
        if isinstance(value,dict) and value not in objects:objects.append(value)
    if len(objects)>1:raise ProviderError(f'Ambiguous WhoScored {key} objects')
    if not objects:raise ProviderError(f'Expected an object for {key}; page data is incomplete')
    return objects[0]


class WhoScoredClient(HttpClient):
    BASE_URL='https://www.whoscored.com'
    REFERER='https://www.whoscored.com/'

    def get_text(self,endpoint,params=None):
        response=self._request(endpoint,params)
        # WhoScored pages are UTF-8. An inferred Windows code page can reject
        # valid player/team names containing bytes such as 0x81.
        content=getattr(response,'content',None)
        return content.decode('utf-8-sig') if isinstance(content,bytes) else response.text

    def player_profile(self,player_id):
        from .playerProfiles import whoscored_profile
        result=whoscored_profile(player_id,self.get_text(f'players/{int(player_id)}/history'))
        return result

    def _directory(self, path, ready):
        # A 200 response can be a partially rendered/cached shell. Refresh that
        # directory once; leave monthly fixture caches reusable on a resume.
        refresh = getattr(self, 'refresh_cache', False)
        try:
            for attempt in range(2):
                parser = _Directory(); parser.feed(self.get_text(path))
                if ready(parser):
                    return parser
                if getattr(self, 'offline', False) or attempt:
                    raise ProviderError('WhoScored directory is incomplete: ' + path)
                self.refresh_cache = True
        finally:
            self.refresh_cache = refresh

    def qualification_path(self,parent_path):
        family=('conference-league' if 'conference-league' in parent_path.lower() else
                'champions-league' if 'champions-league' in parent_path.lower() else 'europa-league')
        parser=self._directory(parent_path, lambda p: any(family in path.lower() and 'qualif' in path.lower() for path in (*p.competition_paths,*p.links)))
        links=set()
        for path in (*parser.competition_paths, *parser.links):
            try: path=directory_path(path)
            except (ValueError, UnicodeError): continue
            if (re.fullmatch(r'regions/250/tournaments/\d+/(?:show/)?[^/]+',path,re.I)
                    and 'qualif' in path and family in path):links.add(path)
        if len(links)!=1:
            raise ProviderError('WhoScored qualifying directory is missing or ambiguous')
        return links.pop()

    def supplemental_path(self, parent_path, tournament_id):
        parser=self._directory(parent_path, lambda p: bool(p.competition_paths))
        paths=set()
        for raw in parser.competition_paths:
            try:path=directory_path(raw)
            except (ValueError,UnicodeError):continue
            if re.fullmatch(r'regions/\d+/tournaments/'+str(int(tournament_id))+r'/(?:show/)?[^/]+',path):paths.add(path)
        if len(paths)!=1:raise ProviderError('WhoScored playoff directory is missing or ambiguous')
        return paths.pop()

    def seasons(self, competition_path):
        parser=self._directory(competition_path, lambda p: bool(p.options))
        return [{'name':name.strip(),'path':directory_path(path)} for path,name in parser.options]

    def season_stages(self, season_path):
        parser=self._directory(season_path,lambda p:bool(published_stages(p,season_path)))
        paths=published_stages(parser,season_path)
        if not paths:raise ProviderError('WhoScored season has no fixture stages')
        if not hasattr(self,'fixture_paths'):self.fixture_paths={}
        if not hasattr(self,'stage_names'):self.stage_names={}
        self.fixture_paths.update(paths)
        for raw,name in parser.stage_names.items():
            found=re.search(r'/stages/(\d+)/',raw,re.I)
            if found and int(found[1]) in paths:self.stage_names[int(found[1])]=name
        return sorted(paths)

    def register_fixture_season(self,competition_path,season):
        """Retain directory context even when stage IDs come from checkpoints."""
        if not hasattr(self,'fixture_seasons'):self.fixture_seasons=[]
        context=(competition_path,season)
        if context not in self.fixture_seasons:self.fixture_seasons.append(context)

    def season_stage(self, season_path):
        """Compatibility helper for domestic competitions with one stage."""
        stages=self.season_stages(season_path)
        if len(stages)!=1:raise ProviderError('Expected one WhoScored league stage')
        return stages[0]

    def season_events(self, stage_id, year_month):
        if not re.fullmatch(r'\d{6}',year_month): raise ValueError('Use YYYYMM')
        payload=self.get(f'tournaments/{int(stage_id)}/data/',{'d':year_month,'isAggregate':'false'})
        tournaments=payload.get('tournaments')
        if not isinstance(tournaments,list): raise ProviderError('WhoScored fixture response is missing tournaments')
        matches=[]
        for tournament in tournaments:
            if tournament.get('stageId') != stage_id: raise ProviderError('WhoScored returned another stage')
            matches.extend(tournament['matches'])
        return matches

    def match_details(self, match_id):
        text=self.get_text(f'matches/{int(match_id)}/live')
        return self.parse_match(text,match_id)

    @classmethod
    def parse_match(cls, text, match_id):
        found=re.search(r'\bmatchId[\"\']?\s*[:=]\s*(\d+)',text)
        if not found or int(found.group(1)) != int(match_id):
            raise ProviderError('WhoScored returned a different or incomplete match page')
        centre=embedded_json(text,'matchCentreData')
        types=embedded_json(text,'matchCentreEventTypeJson')
        if not isinstance(centre.get('events'),list) or not all(centre.get(side,{}).get('players') for side in ('home','away')):
            raise ProviderError('WhoScored event data or lineups are unavailable')
        return {'match_id':int(match_id),'matchCentreData':centre,'matchCentreEventTypeJson':types,
                'source_url':f'{cls.BASE_URL}/matches/{match_id}/live'}

    def match_teams(self, match_id):
        """Read just team IDs/names; lineups and event data are not required."""
        text=self.get_text(f'matches/{int(match_id)}/live')
        found=re.search(r'\bmatchId\s*:\s*(\d+)',text)
        if not found or int(found.group(1))!=int(match_id):
            raise ProviderError('WhoScored returned a different or incomplete match page')
        string=r'(?:\'(?:\\.|[^\'\\])*\'|"(?:\\.|[^"\\])*")'
        header=re.search(
            r'require\.config\.params\[[\'\"]matchheader[\'\"]\]\s*=\s*\{.*?'
            r'\binput\s*:\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*('
            +string+r')\s*,\s*('+string+r')',text,re.S)
        if not header:
            raise ProviderError('WhoScored match page has no team-ID header')
        home_id,away_id,home_name,away_name=header.groups()
        return {'match_id':int(match_id),
                'home':{'id':int(home_id),'name':unescape(ast.literal_eval(home_name))},
                'away':{'id':int(away_id),'name':unescape(ast.literal_eval(away_name))},
                'source_url':f'{self.BASE_URL}/matches/{match_id}/live'}

    def event(self, match_id):
        return self.match_details(match_id)
