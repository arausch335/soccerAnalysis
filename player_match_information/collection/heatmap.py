"""Copy cached Sofascore touch coordinates into the player-match CSV."""
import json
import math
from pathlib import Path

from .jsonFiles import read_json


def extract_heatmap(match, appearance, directory):
    result = {'touch_count': None, 'heatmap_xy': None}
    relative = (match.metadata.get('sofascore_spatial_files', {})
                .get(str(appearance.player.id), {}).get('heatmap'))
    if not relative:
        return result
    directory = Path(directory).resolve()
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError('Heatmap cache path lies outside the collection')
    if not path.exists():
        return result
    saved = read_json(path)
    if saved.get('provider', 'sofascore') != 'sofascore' or saved.get(
            'key', f'heatmap/{match.id}/{appearance.player.id}') != f'heatmap/{match.id}/{appearance.player.id}':
        raise ValueError('Heatmap cache identity mismatch')
    payload = saved.get('payload', saved)
    points = payload.get('heatmap') if isinstance(payload, dict) else None
    if not isinstance(points, list):
        return result
    coordinates = []
    for point in points:
        if not isinstance(point, dict) or any(
                isinstance(point.get(axis), bool) or not isinstance(point.get(axis), (int, float))
                or not math.isfinite(point[axis]) for axis in ('x', 'y')):
            raise ValueError('Heatmap contains an invalid x/y coordinate')
        coordinates.append({'x': point['x'], 'y': point['y']})
    return {'touch_count': len(coordinates),
            'heatmap_xy': json.dumps(coordinates, separators=(',', ':'), allow_nan=False)}
