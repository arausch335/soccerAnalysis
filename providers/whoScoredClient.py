"""WhoScored structured page data, decoded as JSON without executing JavaScript."""
import ast
from html import unescape
from html.parser import HTMLParser
import json
import re
from .httpClient import HttpClient, ProviderError


class _Directory(HTMLParser):
    def __init__(self):
        super().__init__(); self.select=None; self.option=None; self.options=[];self.stage_paths=[];self.links=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='select': self.select=attrs.get('id')
        if tag=='option' and self.select=='seasons': self.option=[attrs.get('value'), '']
        if tag=='option' and self.select=='stages' and attrs.get('value'):
            self.stage_paths.append(attrs['value'])
        if tag=='a' and attrs.get('href'): self.links.append(attrs['href'])

    def handle_data(self, data):
        if self.option is not None: self.option[1]+=data

    def handle_endtag(self, tag):
        if tag=='option' and self.option is not None: self.options.append(self.option); self.option=None
        if tag=='select': self.select=None


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
        if not result['birth_date'] or not result['club'] or not result['career']:
            raise ProviderError('WhoScored profile needs its loaded career table')
        return result

    def qualification_path(self,parent_path):
        parser=_Directory();parser.feed(self.get_text(parent_path))
        family='champions-league' if 'champions-league' in parent_path.lower() else 'europa-league'
        links={path.lstrip('/') for path in parser.links
               if re.fullmatch(r'/regions/250/tournaments/\d+/(?:show/)?[a-zA-Z0-9-]+/?',path,re.I)
               and 'qualif' in path.lower() and family in path.lower()}
        if len(links)>1:raise ProviderError('WhoScored qualifying competition is ambiguous')
        return next(iter(links)) if links else parent_path

    def seasons(self, competition_path):
        parser=_Directory(); parser.feed(self.get_text(competition_path))
        if not parser.options: raise ProviderError('WhoScored season directory is missing')
        return [{'name':name.strip(),'path':unescape(path)} for path,name in parser.options]

    def season_stages(self, season_path):
        parser=_Directory(); parser.feed(self.get_text(season_path))
        # Multi-stage cups expose every stage in the stage <select>; ordinary
        # links usually contain only the currently selected stage.
        paths=[*parser.stage_paths,*parser.links]
        stages=sorted({int(m.group(1)) for path in paths
                       if (m:=re.search(r'/stages/(\d+)/(?:show|fixtures)',path,re.I))})
        if not stages:raise ProviderError('WhoScored season has no fixture stages')
        return stages

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
