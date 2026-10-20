"""Conservative identity resolution constrained to one match and lineup side."""
from pathlib import Path
import json
import re
import unicodedata

ALIASES = {'alisson':'alissonbecker','andyrobertson':'andrewrobertson',
           'kostastsimikas':'konstantinostsimikas','joegomez':'josephgomez',
           'joewillock':'josephwillock','kristofferajer':'kristoffervassbakkajer',
           'amaddiallo':'amad','jair':'jaircunha','samszmodics':'sammieszmodics',
           'hakonrafnvaldimarsson':'hakonvaldimarsson','ilyazabarnyi':'illiazabarnyi',
           'savinho':'savio',
           # Reviewed provider names and nicknames seen in the 2024-26 collection.
           'tymurtutierov':'timurtuterov','linsguether':'linusguther',
           'alfonsoespino':'pacha','pichu':'ivancuellar','rubo':'rubeniranzo',
           'alexandrezurawski':'alemao','gibi':'gibertjordana',
           'gilbertjordana':'gibertjordana',
           'valde':'victorvaldepenas','rafelbauza':'rafaelbauza',
           'javihernandez':'javierhernandez','rubenmunoz':'rubi',
           # Reviewed aliases and nicknames found in the 2023-24 replay.
           'juanbrandariz':'chumi','mathiasjorgensen':'zanka',
           'marezi':'markomilovanovic','pote':'pedrogoncalves',
           'samuelomorodion':'samuaghehowa','vanailson':'vana',
           'vladyslavkochergin':'wladyslawkocherhin',
           'shareefkeouf':'sharifkaiuf','ilaihagag':'ilayhajaj',
           'diaasabia':'diasaba','vitoroliveira':'vitinha',
           'tiagoferreira':'mamede',
           # Reviewed provider variants found in the 2023-24 completion run.
           'shahrudinmahammadaliyev':'shakhrudinmagomedaliyev',
           'sergeyvolkov':'siarheivolkau','sergiygorbunov':'serhiyhorbunov',
           'jacobbarrettlaursen':'jacoblaursen','pedrinho':'pedronaressi',
           'musaaltaamari':'mousatamari','souleymanetoure':'isaaktoure',
           'eliekroupi':'elijuniorkroupi','igorcarioca':'igorsilva',
           # Reviewed variants from the 2024-25 and 2025-26 audit. These are
           # variants for the same player, not genuine lineup disagreements.
           'bhlulmustafazad':'bahlulmustafazada',
           'ericbille':'yveserickbile','neophytosmichael':'neofytosmichail',
           'nrimanakhundzada':'narimanakhundzade','kouadiokone':'manukone',
           'koka':'ahmedhassan','vitaliibuialskyi':'vitaliybuyalskiy',
           'charalamboslykogiannis':'charalamposlykogiannis',
           # Stable display-name variants found while auditing the combined
           # provider-ID concordance.
           'pablocunatcampos':'pablocunat','joshking':'joshuaking',
           'lukadjuric':'lukaduric'}

PLAYER_DISPLAY_NAMES = {
    'michalskoras':'Michał Skóraś',
    'pablocunat':'Pablo Cuñat',
    'joshuaking':'Joshua King',
    'lukaduric':'Luka Đurić',
}

# A cached FotMob lineup for Werder Bremen v Union Berlin on 2024-12-21
# labels Union's starting goalkeeper as Frederik Ronnow.  Contemporary match
# records show Alexander Schwolow started.  Scope the repair to that one match;
# these are different players everywhere else.
REVIEWED_MATCH_PLAYER_IDS = {
    ('fotmob',4534671,'186557'):134778,
}

# Provider-native IDs used when names alone are inherently ambiguous. Porto
# fielded Pepe and Pepê together in 2023-24; accent folding intentionally makes
# both names identical, while these IDs and their shirt numbers remain stable.
REVIEWED_PLAYER_IDS = {
    ('fotmob','25921'):5884,       # Pepe
    ('fotmob','850356'):882664,   # Pepê
    ('fotmob','1290353'):1136280, # Shavy Babicka (older duplicate profile)
    ('fotmob','1586939'):1136280, # Shavy Babicka
    ('whoscored','10105'):5884,   # Pepe
    ('whoscored','338791'):882664, # Pepê
}

# Provider IDs that are duplicate profiles for one player. Normalize these
# while building the cross-provider concordance so one player has one triplet.
REVIEWED_PROVIDER_PLAYER_ID_ALIASES = {
    ('fotmob','1290353'):'1586939', # Warren Shavy / Shavy Babicka
}

# False automatic links found in cached lineups. Each provider player is a
# different person who happened to share a shirt number and one name token
# with the baseline player. Keep the rejection scoped to the exact pair.
REJECTED_PLAYER_ID_LINKS = {
    ('fotmob','1197006','1191206'), # Álvaro Fernández != César Fernández
    ('fotmob','1152741','1133106'), # Adam Toula != Daniel Toula
}

# Provider team pages reviewed after cached fixture indexes were exhausted.
# Keys use the stable Sofascore team ID so same-club rows can be filled across
# competitions and seasons without relying on an ambiguous display name.
REVIEWED_TEAM_IDS = {
    ('fotmob','2588'):'8194',    # Fortuna Düsseldorf
    ('fotmob','6925'):'4120',    # Rodez AF
    ('fotmob','1654'):'9747',    # Guingamp
    ('fotmob','6918'):'47214',   # USL Dunkerque
    ('fotmob','52874'):'6390',   # Red Star FC
}
TEAM_ALIASES = json.loads((Path(__file__).parent/'concordance'/'teams.json').read_text(encoding='utf-8'))

def words(value):
    value=(value.casefold().replace('ı','i').replace('ø','o').replace('ł','l')
           .replace('đ','d'))
    value=unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode()
    return re.findall('[a-z0-9]+',value)

def name_key(name):
    value=''.join(words(name)); return ALIASES.get(value,value)

def canonical_player_name(name):
    """Return a reviewed display name without merging players by name alone."""
    value=str(name).strip()
    return PLAYER_DISPLAY_NAMES.get(name_key(value),value)

def team_key(name,review_root=None):
    from .identityReview import team_aliases
    value=''.join(w for w in words(name) if w not in ('fc','afc','cf','ac'))
    aliases={**TEAM_ALIASES,**team_aliases(review_root)};seen=set()
    while value in aliases and value not in seen:
        seen.add(value);value=aliases[value]
    return value


class IdentityError(ValueError):
    pass


class IdentityResolver:
    """Overrides: {provider: {provider_player_id: baseline_player_id}}."""
    def __init__(self, overrides=None, review_root=None):
        self.overrides=overrides or {};self.review_root=review_root;self.investigator=None

    def player(self, provider, entry, candidates, provider_match_id=None):
        contextual=REVIEWED_MATCH_PLAYER_IDS.get(
            (provider,provider_match_id,str(entry['id'])))
        explicit=self.overrides.get(provider,{}).get(str(entry['id']))
        from .identityReview import player_link,rejected_player_links
        shared=player_link(provider,entry['id'],self.review_root)
        reviewed=REVIEWED_PLAYER_IDS.get((provider,str(entry['id'])))
        if contextual is not None:
            found=[p for p in candidates if p.player.id==int(contextual)]
            method='reviewed_match_id_mapping'
        elif explicit is not None:
            found=[p for p in candidates if p.player.id==int(explicit)]
            method='explicit_id_mapping'
        elif shared is not None:
            found=[p for p in candidates if p.player.id==int(shared)]
            method='dashboard_reviewed_provider_id'
        elif reviewed is not None:
            found=[p for p in candidates if p.player.id==int(reviewed)]
            method='reviewed_provider_id_mapping'
        else:
            found=[p for p in candidates if name_key(p.player.name)==name_key(entry['name'])]
            method='name_or_reviewed_alias'
            if not found and entry.get('shirt_number') is not None:
                tokens=set(words(entry['name']))
                found=[p for p in candidates if str(p.shirt_number)==str(entry['shirt_number'])
                       and tokens.intersection(words(p.player.name))]
                method='unique_shirt_and_shared_name_token'
            found=[p for p in found if
                   (provider,str(entry['id']),str(p.player.id)) not in (REJECTED_PLAYER_ID_LINKS|rejected_player_links(self.review_root))]
        if len(found)!=1:
            raise IdentityError(f'{provider}: cannot uniquely match {entry["name"]} (ID {entry["id"]}); add an identity-map entry')
        player=found[0]
        if entry.get('shirt_number') is not None and player.shirt_number is not None and str(entry['shirt_number'])!=str(player.shirt_number):
            # Providers often retain an old squad number or publish a temporary
            # European number. A unique name/alias/ID match is stronger evidence.
            method += '_with_shirt_conflict_ignored'
        return player,method


def match_fixture(event, fixtures, *, provider=None, review_root=None):
    date=event['startTimestamp']//86400
    wanted=(team_key(event['homeTeam']['name'],review_root),team_key(event['awayTeam']['name'],review_root))
    found=[f for f in fixtures if f['startTimestamp']//86400==date
           and (team_key(f['homeTeam']['name'],review_root),team_key(f['awayTeam']['name'],review_root))==wanted]
    if len(found)!=1:
        if not found and provider:
            from .identityReview import suggest_teams
            suggest_teams(review_root,provider,event,fixtures)
        raise IdentityError(f'Expected one fixture for {event["homeTeam"]["name"]} vs {event["awayTeam"]["name"]}; found {len(found)}')
    return found[0]
