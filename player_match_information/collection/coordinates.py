"""Copy provider-reported average positions and raw touch coordinates."""
import json
import math
from pathlib import Path

from .jsonFiles import read_json


def _cached_payload(directory, relative, key):
    if not relative:
        return None
    directory = Path(directory).resolve()
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError('Spatial cache path lies outside the collection')
    if not path.exists():
        return None
    saved = read_json(path)
    if saved.get('provider', 'sofascore') != 'sofascore' or saved.get(
            'key', key) != key:
        raise ValueError('Spatial cache identity mismatch')
    return saved.get('payload', saved)


def _coordinate(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def extract_average_position(match, appearance, directory):
    result = {'average_x': None, 'average_y': None}
    relative = match.metadata.get('sofascore_spatial_files', {}).get('average_positions')
    payload = _cached_payload(directory, relative, f'average_positions/{match.id}')
    if not isinstance(payload, dict):
        return result
    side = appearance.lineup.side.value
    entries = payload.get(side)
    if not isinstance(entries, list):
        return result
    matches = [entry for entry in entries if isinstance(entry, dict)
               and str((entry.get('player') or {}).get('id')) == str(appearance.player.id)]
    if len(matches) > 1:
        raise ValueError('Average-position cache has duplicate player IDs')
    if not matches:
        return result
    entry = matches[0]
    for source, output in (('averageX', 'average_x'), ('averageY', 'average_y')):
        value = entry.get(source)
        if value is not None and not _coordinate(value):
            raise ValueError('Average position contains an invalid x/y coordinate')
        result[output] = value
    return result


def extract_heatmap(match, appearance, directory):
    result = {'touch_count': None, 'heatmap_xy': None}
    relative = (match.metadata.get('sofascore_spatial_files', {})
                .get(str(appearance.player.id), {}).get('heatmap'))
    payload = _cached_payload(directory, relative, f'heatmap/{match.id}/{appearance.player.id}')
    points = payload.get('heatmap') if isinstance(payload, dict) else None
    if not isinstance(points, list):
        return result
    coordinates = []
    for point in points:
        if not isinstance(point, dict) or any(
                not _coordinate(point.get(axis)) for axis in ('x', 'y')):
            raise ValueError('Heatmap contains an invalid x/y coordinate')
        coordinates.append({'x': point['x'], 'y': point['y']})
    return {'touch_count': len(coordinates),
            'heatmap_xy': json.dumps(coordinates, separators=(',', ':'), allow_nan=False)}
