"""Recover missing WhoScored player aliases from the same matched lineup side."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from providers.cache import ResponseCache
from providers.identity import (
    REJECTED_PLAYER_ID_LINKS, REVIEWED_MATCH_PLAYER_IDS, REVIEWED_PLAYER_IDS,
    name_key, words,
)
from providers.identityReview import player_link, rejected_player_links
from providers.reep import index
from providers.scores import whoscored_scores

from .identityReferences import accepted_aliases


def _cached_match(cache, row, aliases, provider, prefix, registry):
    entity = row['entity_id']
    explicit = str(row.get(provider + '_match_id') or '')
    if explicit and registry.lookup('match', provider, explicit) != entity:
        return '', None, 'conflicting_' + provider + '_match_id'
    natives = set(aliases.get(provider + '_id', ()))
    if explicit:
        natives.add(explicit)
    paths = {native: cache.path(provider, prefix + native) for native in natives}
    saved = [native for native, path in paths.items() if path.is_file()]
    if explicit in saved:
        saved = [explicit]
    if len(saved) != 1:
        return '', None, ('ambiguous_' if saved else 'missing_') + provider + '_response'
    native = saved[0]
    envelope = json.loads(paths[native].read_text(encoding='utf-8-sig'))
    if envelope.get('provider') != provider or envelope.get('key') != prefix + native:
        raise ValueError(f'Player bridge raw response identity mismatch: {paths[native]}')
    payload = envelope.get('payload')
    if not isinstance(payload, dict):
        return native, None, 'invalid_' + provider + '_response'
    return native, payload, ''


def _roster(entries, provider):
    players = {}
    for entry in entries:
        if provider == 'sofascore':
            person = entry.get('player') or {}
            pid, name = person.get('id'), person.get('name')
            shirt = entry.get('shirtNumber', entry.get('jerseyNumber'))
        else:
            pid, name = entry.get('playerId'), entry.get('name')
            shirt = entry.get('shirtNo')
        pid = str(pid or '')
        if pid.isdigit():
            players[pid] = (str(name or ''), str(shirt or ''))
    return players


def _lineup_claims(baseline, other):
    # Count the whole roster before resolving candidates to canonical IDs.
    # An unregistered same-name teammate can still make a name ambiguous.
    sofa_names, who_names = defaultdict(list), defaultdict(list)
    for pid, (name, _) in baseline.items():
        if key := name_key(name):
            sofa_names[key].append(pid)
    for pid, (name, _) in other.items():
        if key := name_key(name):
            who_names[key].append(pid)
    sofa_shirts = Counter(shirt for _, shirt in baseline.values() if shirt)
    who_shirts = Counter(shirt for _, shirt in other.values() if shirt)
    sofa_by_shirt = {shirt: pid for pid, (_, shirt) in baseline.items() if shirt}
    exact_targets = {ids[0] for key, ids in sofa_names.items() if len(ids) == len(who_names.get(key, ())) == 1}
    for pid, (name, shirt) in other.items():
        key = name_key(name)
        candidates = sofa_names.get(key, ())
        if candidates:
            if len(candidates) == len(who_names[key]) == 1:
                yield pid, candidates[0], 'unique_lineup_name'
            else:
                yield pid, '', 'ambiguous_lineup_name'
        elif shirt and sofa_shirts[shirt] == who_shirts[shirt] == 1:
            candidate = sofa_by_shirt[shirt]
            name_parts, baseline_parts = words(name), words(baseline[candidate][0])
            if (candidate not in exact_targets and name_parts and baseline_parts
                    and name_parts[-1] == baseline_parts[-1]):
                yield pid, candidate, 'unique_shirt_and_shared_name_token'


def recover_lineup_aliases(root, rows, players, registry, *, trace_player=None):
    """Attach validated lineup evidence to existing identities."""
    recovered, conflicts = [], []
    skipped = Counter()
    report = {'recovered': recovered, 'conflicts': conflicts, 'skipped': skipped}
    if trace_player:
        report['lineup_observations'] = []
    if registry is None:
        return report
    root = Path(root)
    allowed = {r['entity_id'] for r in players if r.get('entity_id')}
    aliases = defaultdict(set)
    for token, alias in registry.value['aliases'].items():
        if token.startswith('player|') and token not in registry.value.get('withheld', {}):
            entity = registry.canonical(alias)
            if entity in allowed:
                aliases[entity].add(alias)
    match_aliases = accepted_aliases(registry, 'match')
    withheld = registry.value.get('withheld', {})
    claims = defaultdict(lambda: {'entities': set(), 'examples': [], 'confirmed_shirt_match': False})
    disputed = set()
    rejected = REJECTED_PLAYER_ID_LINKS | rejected_player_links(root)
    bridges = index(root)
    for row in rows:
        match = row.get('entity_id')
        validation = row.get('whoscored_fixture_check') or ''
        if not match or validation not in ('', 'missing', 'bridge_only', 'validated'):
            skipped['unusable_fixture'] += 1
            continue
        competition = re.sub(r'[^a-z0-9]+', '-', str(row.get('competition', '')).casefold()).strip('-')
        season = str(row.get('season', '')).replace('/', '-')
        if not competition or not re.fullmatch(r'\d{4}-\d{4}', season):
            skipped['missing_fixture_scope'] += 1
            continue
        cache = ResponseCache(root / competition / season / 'raw')
        who, payload, reason = _cached_match(cache, row, match_aliases.get(match, {}),
                                            'whoscored', 'match/', registry)
        if reason:
            skipped[reason] += 1
            continue
        _, baseline, reason = _cached_match(cache, row, match_aliases.get(match, {}),
                                            'sofascore', 'lineups/', registry)
        if reason:
            skipped[reason] += 1
            continue
        centre = payload.get('matchCentreData', payload)
        if not isinstance(centre, dict):
            skipped['invalid_match_payload'] += 1
            continue
        native_match = payload.get('match_id') or centre.get('matchId') or who
        date = centre.get('startDate') or centre.get('startTime') or ''
        if (str(native_match) != who or str(centre.get('statusCode')) != '6'
                or not row.get('date') or str(date)[:10] != str(row['date'])[:10]):
            skipped['invalid_match_payload'] += 1
            continue
        sides = {}
        for side in ('home', 'away'):
            team = row.get(side + '_entity_id')
            native_team = str(row.get('sofascore_' + side + '_team_id') or '')
            other = centre.get(side) or {}
            if (team and registry.lookup('team', 'sofascore', native_team) == team
                    and registry.lookup('team', 'whoscored', str(other.get('teamId') or '')) == team
                    and row.get(side + '_team_identity_status') != 'conflict'):
                sides[side] = other
        if validation != 'validated':
            # Older catalogues can carry an accepted fixture alias without its
            # validation label. Recheck the saved evidence before using it.
            try:
                score_matches = all(row.get(side + '_score') in (None, '') or
                                    str(whoscored_scores(sides[side].get('scores') or {})['display'])
                                    == str(row[side + '_score']) for side in sides)
            except (TypeError, ValueError):
                score_matches = False
            if (len(sides) != 2 or row.get('home_entity_id') == row.get('away_entity_id')
                    or not centre.get('events') or not score_matches):
                skipped['fixture_validation_failed'] += 1
                continue
        if not sides:
            skipped['fixture_team_disagreement'] += 1
        for side, other in sides.items():
            sofa_players = _roster((baseline.get(side) or {}).get('players', []), 'sofascore')
            who_players = _roster(other.get('players', []), 'whoscored')
            # A match can contain an event actor omitted from its lineup list.
            # The dictionary supplies the name; the event supplies the side.
            names = centre.get('playerIdNameDictionary') or payload.get('playerIdNameDictionary') or {}
            if not isinstance(names, dict):
                names = {}
            for event in (centre.get('events') or ()) if names else ():
                if str(event.get('teamId')) != str(other.get('teamId')):
                    continue
                native = str(event.get('playerId') or '')
                if native.isdigit() and names.get(native):
                    who_players.setdefault(native, (names[native], ''))
            if trace_player in who_players:
                name, shirt = who_players[trace_player]
                report['lineup_observations'].append({
                    'provider_match_id': who, 'side': side, 'name': name, 'shirt': shirt,
                    'sofascore_candidates': [
                        {'id': pid, 'name': other_name, 'shirt': other_shirt,
                         'entity_id': registry.lookup('player', 'sofascore', pid)}
                        for pid, (other_name, other_shirt) in sofa_players.items()
                        if (name_key(name) and name_key(name) == name_key(other_name))
                        or (shirt and shirt == other_shirt)
                    ],
                })
            for native, baseline_id, method in _lineup_claims(sofa_players, who_players):
                example = {'match_entity_id': match, 'provider_match_id': who, 'side': side,
                           'player_name': who_players[native][0], 'method': method}
                if not baseline_id:
                    disputed.add(native)
                    conflicts.append({'provider_player_id': native, 'reason': method, **example})
                    continue
                entity = registry.lookup('player', 'sofascore', baseline_id)
                if entity not in aliases:
                    disputed.add(native)
                    skipped['no_published_sofascore_player'] += 1
                    continue
                approved = (REVIEWED_MATCH_PLAYER_IDS.get(('whoscored', int(who) if who.isdigit() else who, native))
                            or player_link('whoscored', native, root)
                            or REVIEWED_PLAYER_IDS.get(('whoscored', native)))
                if (('whoscored', native, baseline_id) in rejected
                        or (approved is not None and str(approved) != baseline_id)
                        or bridges.same('player', 'whoscored', native, 'sofascore', baseline_id) is False):
                    disputed.add(native)
                    conflicts.append({'provider_player_id': native,
                                      'reason': 'rejected_or_conflicting_player_link', **example})
                    continue
                claim = claims[native]
                claim['entities'].add(entity)
                # A quarantine needs stronger evidence than a new name-only link.
                if (withheld.get('player|whoscored|' + native) == 'ambiguous_provider_link'
                        and validation == 'validated' and baseline.get('confirmed') is True
                        and method == 'unique_lineup_name'):
                    shirt = sofa_players[baseline_id][1]
                    known = {bridges.canonical_id(v) for v in bridges.ids('player', 'whoscored', native)}
                    if (shirt and who_players[native][1] == shirt and (not known or known == {entity})
                            and all(sum(number == shirt for _, number in roster.values()) == 1
                                    for roster in (sofa_players, who_players))):
                        claim['confirmed_shirt_match'] = True
                if len(claim['examples']) < 3:
                    claim['examples'].append(example)
    # Collect every fixture claim before accepting a global provider alias.
    for native, claim in sorted(claims.items()):
        token = 'player|whoscored|' + native
        targets = claim['entities']
        held = withheld.get(token)
        releasable = (held == 'ambiguous_provider_link' and claim['confirmed_shirt_match']
                      and native not in disputed)
        if len(targets) != 1:
            reason = 'conflicting_lineup_identities'
        elif token in withheld and not releasable:
            reason = 'withheld_provider_mapping'
        else:
            entity = next(iter(targets))
            existing = registry.lookup('player', 'whoscored', native)
            if existing and existing != entity:
                reason = 'existing_mapping_disagrees'
            elif existing == entity and not releasable:
                continue
            else:
                if releasable:
                    withheld.pop(token)
                registry.value['aliases'][token] = min(aliases[entity])
                recovered.append({'provider': 'whoscored', 'provider_player_id': native,
                                  'entity_player_id': entity, 'examples': claim['examples'],
                                  **({'cleared_withheld_reason': held} if releasable else {})})
                continue
        conflicts.append({'provider_player_id': native, 'candidate_entities': sorted(targets),
                          'reason': reason, 'examples': claim['examples']})
    return report


def main():
    """Trace one WhoScored fixture against a local bundle without publishing."""
    import argparse
    import config
    from .publication import folder, read_registry, reference_rows, wide_rows
    from .registry import Registry

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--data-root', type=Path, default=config.DATA_ROOT)
    parser.add_argument('--dataset-root', type=Path, default=config.DATASET_ROOT)
    parser.add_argument('--match-id', required=True)
    parser.add_argument('--player-id')
    args = parser.parse_args()
    registry = Registry(args.data_root/'_system/identity/alex_ids.json',
                        value=read_registry(folder(args.dataset_root)))
    match = registry.lookup('match', 'whoscored', args.match_id)
    rows = [row for row in wide_rows(args.dataset_root)
            if (match and row['entity_id'] == match)
            or row.get('whoscored_match_id') == args.match_id]
    result = {'scope': 'single_fixture_diagnostic', 'fixture_rows': len(rows), 'match_entity_id': match}
    if args.player_id:
        result['published_player_entity_id'] = registry.lookup('player', 'whoscored', args.player_id)
        result['withheld_reason'] = registry.value.get('withheld', {}).get('player|whoscored|' + args.player_id)
    result.update(recover_lineup_aliases(args.data_root, rows,
                  reference_rows(args.dataset_root, 'player'), registry, trace_player=args.player_id))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
