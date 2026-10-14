"""Render actual cached heat points, passes and dribbles without HTTP."""
import argparse
from pathlib import Path
import os
os.environ.setdefault(
    'MPLCONFIGDIR', str(Path(__file__).resolve().parents[1] / 'outputs' / '.cache' / 'matplotlib')
)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle

from collection.spatial import _payload, _point, extract_pass_features, extract_dribble_features, extract_spatial_features
from storage import PandasStore


def render(directory, match_id, player_id):
    directory = Path(directory)
    dataset = PandasStore(directory / 'match_checkpoints' / str(match_id)).from_csv()
    match = dataset.matches[match_id]
    appearance = next(p for lineup in match.lineups for p in lineup.players if p.player.id == player_id)
    heat = extract_spatial_features(match, appearance, directory)
    ps, passes, _ = extract_pass_features(match, appearance, directory)
    ds, dribbles = extract_dribble_features(match, appearance, directory, heat)
    files = match.metadata.get('sofascore_spatial_files', {}).get(str(player_id), {})
    payload = _payload(directory, files.get('heatmap')) or {}
    points = [p for raw in payload.get('heatmap', []) if (p := _point(raw))]
    fig, axes = plt.subplots(1, 3, figsize=(16, 6), facecolor='#f4f6f8')
    for ax in axes:
        ax.set_facecolor('#eaf1e8')
        ax.add_patch(Rectangle((0, 0), 100, 100, fill=False, color='#65756a'))
        ax.plot([50, 50], [0, 100], color='#65756a', lw=1)
        ax.add_patch(Circle((50, 50), 9.15/1.05, fill=False, color='#65756a'))
        for x in (0, 84.3):
            ax.add_patch(Rectangle((x, 20.35), 15.7, 59.3, fill=False, color='#65756a'))
        ax.set(xlim=(-2, 102), ylim=(-2, 102), aspect=68/105)
        ax.set_xlabel('Provider x (0–100)')
        ax.set_ylabel('Provider y (0–100)')
    axes[0].set_title('Touch heat map · ' + str(heat['heatmap_status']))
    if points:
        axes[0].hexbin(*zip(*points), gridsize=16, extent=(0,100,0,100), mincnt=1, cmap='YlOrRd', alpha=.8)
    else:
        axes[0].text(50,50,'No touch coordinates available',ha='center',wrap=True)
    axes[1].set_title('Pass map · ' + str(ps['pass_map_status']))
    for row in passes:
        axes[1].annotate('', xy=(row['destination_x_raw'],row['destination_y_raw']),
                         xytext=(row['origin_x_raw'],row['origin_y_raw']),
                         arrowprops=dict(arrowstyle='->',lw=1.2,alpha=.7,
                                         color='#18785b' if row['completed'] else '#bd4938'))
    if not passes: axes[1].text(50,50,'No mapped passes',ha='center')
    axes[2].set_title('Dribble map · ' + str(ds['dribble_map_status']))
    for row in dribbles:
        axes[2].scatter(row['x_raw'],row['y_raw'],s=55,
                        marker='o' if row['successful'] else 'x',
                        color='#18785b' if row['successful'] else '#bd4938')
    if not dribbles: axes[2].text(50,50,'No mapped dribbles',ha='center')
    fig.suptitle(f'{appearance.player.name} · {match.home_team.name} vs {match.away_team.name}',fontsize=16)
    fig.text(.5,.025,'Green = successful · Red = unsuccessful · Coordinates shown as supplied; no inferred movement paths.',ha='center')
    fig.tight_layout(rect=(0,.06,1,.95))
    output = directory/'maps'/f'{match_id}_{player_id}.png'
    output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(output,dpi=160)
    plt.close(fig)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('match_id', type=int)
    parser.add_argument('player_id', type=int)
    args = parser.parse_args()
    print(render(args.directory,args.match_id,args.player_id))
