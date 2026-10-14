# Player action maps and match replay

In PyCharm, use module name `analysis.match_visualization` in a Python run configuration with the repository root as its working directory. It rebuilds the offline explorer from the current local enriched export and opens it in your browser. No package installation, server, network access, or Codex runtime is required.

From the project directory:

```sh
.venv/bin/python -m analysis.match_visualization
.venv/bin/python -m analysis.match_visualization --no-open
```

Open `outputs/match-visualization/match-explorer.html` directly to view the last build without rebuilding. Each build writes a provenance manifest next to it. Paths are anchored to the project, so the IDE working directory does not matter.

The input is `outputs/event-layer-full/events_enriched.csv`. Refresh it with `python -m analysis.event_layer` before rebuilding the explorer to include newer collected events. The builder also reads cached WhoScored match payloads under `data/premier-league/2024-2025/raw/whoscored` to identify home and away teams. It does not run or change the collector.

Use `--source`, `--raw-dir`, and `--output` to override these paths. For example:

```sh
.venv/bin/python -m analysis.match_visualization --source outputs/event-layer-trial/events_enriched.csv --output outputs/match-visualization-trial
```

## Views

- **Player match map:** choose a match and player; combine passes, take-ons, shots, defensive actions and other events. Inspect actions through the selector or pitch. Enable event-density heatmap and separately labeled inferred carries.
- **Match replay:** play/pause, step between events, scrub the timeline, and choose 0.5×, 1×, 2×, 4×, 8×, 16×, or 32×. At 1×, one recorded second elapses per wall-clock second. A 12-second trail shows recent actions.

## Data interpretation

Player maps align attacks left to right in both halves. Replay uses fixed ends: home attacks right and away attacks left, rotating away coordinates 180 degrees. Actual stadium orientation is not inferred.

Only explicit pitch endpoints produce arrows; events with missing endpoints remain points. Shot goal-mouth coordinates use a different projection and are not substituted. Administrative events retain time and descriptions without artificial locations. Provider periods outside normal/extra-time halves are excluded.

Take-ons generally describe a dribble attempt location. Dotted carries are inferred pass-end-to-receiver-action gaps from the enrichment layer, not observed runs. The heatmap is a relative Gaussian-smoothed density of all recorded player action origins, evaluated on a 24×16 grid; it is not time spent, and action visibility filters do not change its population.

Replay omits the halftime interval but retains the provider's regulation/stoppage-time clock. Simultaneous events retain provider order for stepping; playback selects the latest at that timestamp. Score is derived from recorded Goal events with own-goal attribution reversed. Arrow travel speed is illustrative. No continuous tracking or off-ball movement is reconstructed.

`build.py`, `viewer.html`, and `viewer.css` are all local to this folder. The stylesheet is bundled to keep the explorer independent of the Codex installation.
