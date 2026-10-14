# Season role analysis

In PyCharm, create a Python run configuration with module name `analysis.role_pipeline` and the repository root as its working directory. It reads existing local files, creates outputs under `outputs/role-analysis`, and opens the interactive report. It does not collect data or change source datasets.

```sh
.venv/bin/python -m analysis.role_pipeline
.venv/bin/python -m analysis.role_pipeline --no-open
.venv/bin/python -m analysis.role_pipeline --min-minutes 900 --min-matches 10
```

Dependencies: existing pandas, NumPy and SciPy, plus scikit-learn 1.9.0 and Plotly 7.0.0 (recorded in `requirements-role-analysis.txt`). The page is self-contained and works offline.

## Inputs and joins

- `data/premier-league/2024-2025/player_match_stats.csv`: canonical player-match statistics plus touch count and average x/y.
- `data/premier-league/2024-2025/identities.csv`: existing match-specific WhoScored-to-canonical player bridge. New joins never use names. Ambiguous non-null bridges and duplicate canonical appearances stop processing.
- `data/premier-league/2024-2025/raw/whoscored`: cached formation records and player identities.
- `outputs/event-layer-full/events_enriched.csv`: source events, pitch geometry, field regions and explicitly labeled inference.
- `features.py`: the project's existing ROLE and PERFORMANCE metric contract. Every role metric is retained or listed as unavailable. Additional regional and geometric features extend it.

Override locations with `--project-root`, `--data-dir`, `--event-dir`, and `--output`. The defaults are relative to the project, independent of the IDE working directory. `--competition` supplies the competition label for a single-competition input directory. Never mix competitions in that directory. Input fingerprints are checked so a concurrent export change stops publication; rerun against the completed export.

Refresh the enriched event layer with `python -m analysis.event_layer` when new collector exports are ready, then rerun this analysis. The manifest reports observed versus scheduled match coverage. Aggregation of an incomplete season remains an observed-season profile.

## Analysis contract

### Detailed positions

Formation name and slot are mapped to the starting lineup's explicit provider position label. All observed labels must agree for a formation/slot pair; ambiguous or unseen slots remain unknown. Substitutes inherit their recorded occupied formation slot, not a guessed position from their event locations. Formation stints provide position-use shares. An explicit starting-position fallback is labeled if no usable stint exists.

Position groups: goalkeeper, centre-back, full-back/wing-back, central midfield, attacking midfield, winger/wide midfield, striker. Side and raw position labels remain in the source and stint tables. A match with no dominant position above 60% is kept as mixed/uncertain. Match statistics are assigned wholly to the dominant match position; this does not claim exact per-stint action or minute statistics.

Each player has an all-position season profile and separate profiles formed from the matches assigned to each position. Thus a multi-position player can appear in multiple position comparisons without combining their striker and full-back matches. Position-use minutes are normalized within each appearance from the source formation-duration shares, and are estimates when provider stoppage-time and official minutes differ.

### Aggregation and missingness

Count rates: `90 × sum(count) / sum(minutes where count is observed)`. Missing is never treated as zero. Ratios sum compatible observed numerators and denominators before division; zero opportunities produce an unknown ratio. Mean heatmap summaries use source heat-point counts as weights; ordinary context means use minutes. Per-feature observed minutes and denominators are exported for every profile.

Event counts can be zero only if the match has both halves and their end markers, the player identity is bridged, and the player is in the raw roster. Otherwise event metrics are missing. End markers establish an operational coverage check, not proof that every individual provider action is correctly coded. Provider count differences are exported and never silently blended.

Eight regional distributions use the 3×5 grid: open-play on-ball origins, pass origins, pass destinations, take-ons, shots, defensive interventions, ball recoveries and inferred receptions. Empty action families have no distribution. Full 15-cell counts are exported per match and shares per season. All attack directions are normalized; small provider y means the right side.

Existing provider heatmap summaries remain separate from event geography. The event locations describe where actions happened, not time spent, off-ball positioning, pressure or marking. Carries and medium/high-confidence receptions from the enrichment layer are explicitly inferred and excluded from the default distance model.

### Comparison

Defaults: at least 450 minutes and 5 matches in the position, 80% event-minute coverage, and at least 16 sufficiently covered peers for modeling. Profiles below threshold remain inspectable but are not clustering training observations. A player can have a season overview without an eligible detailed-position comparison.

Percentiles use each metric's observed eligible peers only; at least 8 observations are required. A high role percentile means more activity, not superior execution. Pairwise similarity uses the same supported, scaled role features as the clustering matrix. Neighbors are drawn exclusively from the same competition, season and detailed position.

Goal totals, assists, ratings, completion percentages, adverse outcomes and unverified physical units are context rather than default role-model inputs. The default candidate list is explicit in `config.py`. All original metrics remain available for inspection even when excluded from the model. Team style and opposition remain confounders; no possession-adjusted ability or causal tactical conclusions are asserted.

### Clustering and sensitivity

For each position, retain supported features with 80% profile coverage and adequate variation; remove within-block Spearman correlations above 0.90. Each profile must have 80% of retained features observed. Only the model matrix fills residual missing values using that cohort's medians; observed outputs stay missing and every imputed cell is flagged.

Counts receive log1p transforms; folded region shares receive square roots. Values are winsorized at cohort 1st/99th percentiles, centered by the median and scaled by IQR (standard deviation fallback), clipped to ±4 scaled units. Each conceptual block is normalized to equal total variance to prevent a large group of region variables from dominating every other family. Left/right regions are folded into wide/half-space/central bands for the model, while original 15-region maps remain unchanged.

K-means (30 initializations, fixed seed) and Ward hierarchical clustering are compared for 2–6 groups subject to sample size. Clusters must contain at least 5 players or 5% of the cohort, whichever is larger. The admissible candidate with greatest silhouette is retained as exploratory. Silhouette below 0.15 is flagged weak; mean match-bootstrap ARI below 0.60 is flagged unstable. These are transparent screening rules, not validated football role thresholds. There is no requirement that a discrete role partition exists.

Stability uses 20 resamples of matches within each player-position profile; the aggregation, scaling and clustering are recomputed with the same feature set and cluster count. The chosen candidate is checked, not every possible model, so this is conditional stability, not unbiased model-selection validation. Raw-feature 10th/90th match-bootstrap intervals use 200 resamples. Team/cluster adjusted mutual information is reported as a confounding check. Neutral numbered groups get measured feature contrasts rather than predetermined tactical names.

PCA coordinates remain available in the exports. Similarities and saved cluster fitting use the full selected matrix. Fitted transforms, PCA parameters and selected features are saved in `models.json`; the seed and thresholds are in `manifest.json`.

## Interactive 3D comparison

Open the **3D comparison** tab, select a detailed position, and choose the X, Y and Z variables. Options include role metrics, event-region shares and clearly marked performance/sample context. Drag to rotate, scroll or pinch to zoom, and use **Reset view** to restore the camera. Hover for values; click a player to inspect them. The expandable player table provides the same selection controls.

**Saved role clusters** colors players by the full role model described above; axis changes do not refit that model. **Cluster these three variables** fits exploratory K-means using the three selected axes standardized to equal variance, with 2–6 groups, 16 deterministic initializations and a silhouette score. These custom groups are recalculated in the browser and do not replace saved analysis outputs or carry the saved model's bootstrap stability assessment. Performance variables can be explored here but are not silently added to the saved role model.

By default the plot includes profiles meeting the comparison sample thresholds. **Include limited samples** adds the other players in that position, marked as diamonds. Players missing any selected value are omitted from the plot and counted explicitly; missing values are never plotted as zero. Custom clustering uses all complete profiles currently included by that toggle. Selecting the same variable twice disables custom grouping.

Plotly is embedded in the generated HTML, so no server or internet connection is needed. Open `role-explorer.html#comparison` to go directly to this tab.

## Outputs

- `player_match_roles.csv`: original canonical match context, mapped positions, event counts/regions, and role rates.
- `player_season_roles.csv` and `player_role_profiles.jsonl`: all-position observed-season profiles and evidence-based descriptions for every player.
- `player_position_roles.csv`: position-conditioned profiles, eligibility, comparisons and exploratory labels.
- `event_regions_player_match.csv`, `position_stints.csv`, `formation_slot_dictionary.csv`: spatial/position evidence.
- `season_metric_coverage.csv`, `metric_catalog.csv`, `match_event_coverage.csv`, `provider_count_comparison.csv`, `identity_unlinked_event_players.csv`: coverage, denominators and join/source audits.
- `comparison_percentiles.csv`, `nearest_players.csv`: within-position comparisons.
- `role_model_matrix.csv`, `model_feature_selection.csv`, `cluster_diagnostics.csv`, `cluster_profiles.csv`, `season_metric_uncertainty.csv`, `models.json`: reproducible modeling evidence.
- `analysis.md`, `role-explorer.html`, `manifest.json`: reviewable report and run provenance.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_role_pipeline.py' -q
```

Tests cover denominator-aware rates, missingness, pooled count ratios, coordinate boundaries, nonmutating model imputation and substitute/unavailable formation slots. Full-run audits additionally verify unique player profiles, within-position neighbor membership, probability sums, performance-feature exclusions and deterministic outputs.

Method references: [clustering](https://scikit-learn.org/stable/modules/clustering.html), [robust scaling](https://scikit-learn.org/stable/modules/generated/sklearn.preprocessing.RobustScaler.html).
