"""Explore saved player-match data without making network requests.

Run this file in the IDE, or use:
    python eda.py --data data/premier-league/2024-2025
    python eda.py --team Liverpool --min-minutes 450

Outputs: an offline interactive report and tidy CSV analysis tables. The input
CSVs are never changed. Functions return DataFrames for further IDE exploration.
Role is described by activity; performance by outcomes and execution. This file
does not estimate traits, causal effects, or a composite player score.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px

from api.statistics import PAIRS, SCALARS


PROJECT = Path(__file__).resolve().parent
DEFAULT_DATA = PROJECT / "data" / "premier-league" / "2024-2025"
KEY = ["match_id", "player_id"]
PLAYER = ["season", "team", "player_id", "player_name"]
POSITIONS = {"G": "Goalkeeper", "GK": "Goalkeeper", "D": "Defender",
             "M": "Midfielder", "F": "Forward"}
RAW_METRICS = list(SCALARS) + ["shots_inside_box", "shots_outside_box"] + [
    f"{stem}_{part}" for stem in PAIRS for part in ("attempted", "completed")]
RATE_METRICS = [f"{stem}_completion_rate" for stem in PAIRS]
ACTIVITY = ["touches", "passes_attempted", "long_balls_attempted",
            "crosses_attempted", "through_balls_attempted", "dribbles_attempted",
            "shots", "touches_opposition_box", "passes_into_final_third",
            "line_breaking_passes", "tackles", "interceptions", "recoveries",
            "ground_duels_attempted", "aerial_duels_attempted", "sprints",
            "progressive_carries", "progressive_carry_distance_m"]
OUTCOMES = ["goals", "assists", "xg", "xgot", "xa", "shots_on_target",
            "dispossessed", "possession_lost", "goals_prevented"]
PER90 = ACTIVITY + OUTCOMES
CORRELATION_METRICS = ["shots", "xg", "goals", "xa", "key_passes",
                       "passes_attempted", "dribbles_attempted", "tackles"]
COUNT_METRICS = {m for m, spec in SCALARS.items() if spec[-1] == "count"} | {
    m for m in RAW_METRICS if m.endswith(("_attempted", "_completed"))} | {
    "shots_inside_box", "shots_outside_box"}


def boolean(series: pd.Series) -> pd.Series:
    """Parse booleans explicitly: bool('False') would incorrectly be True."""
    return series.astype("string").str.lower().map(
        {"true": True, "false": False, "1": True, "0": False}).astype("boolean")


def read_optional(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, **kwargs)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_data(path: Path) -> tuple[pd.DataFrame, Path, dict]:
    """Accept either a collection folder or its player CSV; do not pool datasets."""
    if path.is_dir():
        candidates = [path / "player_match_stats.csv", path / "all_players.csv"]
        path = next((p for p in candidates if p.exists()), candidates[0])
    if not path.exists():
        raise ValueError(f"Player CSV not found: {path}. Select a saved dataset with --data.")
    frame = pd.read_csv(path, low_memory=False).copy()
    required = set(KEY + ["season", "date", "team", "opponent", "player_name",
                          "starter", "minutes_played"])
    if missing := required - set(frame):
        raise ValueError(f"Unsupported player CSV; missing columns: {sorted(missing)}")
    if "position" not in frame:
        frame["position"] = frame.get("position_sofascore", "Unknown")
    meta_path = path.parent / "run_summary.json"
    metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return frame, path, metadata


def prepare_data(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Audit before analysis. Keep suspicious records visible in quality_issues.

    Ambiguous duplicate identities and missing context are excluded. Invalid
    metric cells become missing in the analysis copy; no missing values become 0.
    Plausible outliers and failed cross-field reconciliations are only flagged.
    """
    df = raw.copy()
    issues = []

    def flag(mask, check, metric, severity="warning"):
        mask = pd.Series(mask, index=df.index).fillna(False)
        if mask.any():
            selected = df.loc[mask, KEY + ["player_name", "team"]].copy()
            selected["check"] = check
            selected["metric"] = metric
            selected["severity"] = severity
            selected["value"] = df.loc[mask, metric].astype(str) if metric in df else ""
            issues.append(selected)

    duplicated = df.duplicated(KEY, keep=False)
    missing_context = df[KEY + ["team", "season", "player_name", "opponent"]].isna().any(axis=1)
    flag(duplicated, "duplicate player-match key: all copies excluded", "", "error")
    flag(missing_context, "missing identity/context: row excluded", "", "error")
    dates = pd.to_datetime(df["date"], errors="coerce", utc=True)
    flag(dates.isna(), "invalid date: row excluded", "date", "error")
    df["date"] = dates
    df["starter"] = boolean(df["starter"])
    flag(df.starter.isna(), "unrecognized starter flag", "starter")
    for metric in [m for m in RAW_METRICS + RATE_METRICS if m in df]:
        original = df[metric]
        numeric = pd.to_numeric(original, errors="coerce")
        bad = original.notna() & (numeric.isna() | ~np.isfinite(numeric))
        flag(bad, "invalid numeric value: excluded", metric, "error")
        df[metric] = numeric.mask(bad)
        negative = df[metric].lt(0) if metric != "goals_prevented" else pd.Series(False, index=df.index)
        flag(negative, "negative nonnegative metric: excluded", metric, "error")
        df.loc[negative, metric] = np.nan
        if metric in COUNT_METRICS:
            fractional = df[metric].notna() & df[metric].mod(1).abs().gt(1e-8)
            flag(fractional, "fractional event count: excluded", metric, "error")
            df.loc[fractional, metric] = np.nan
    flag(df.minutes_played.gt(130), "minutes above 130: inspect, retained", "minutes_played")
    for stem in PAIRS:
        a, c, r = f"{stem}_attempted", f"{stem}_completed", f"{stem}_completion_rate"
        if a not in df or c not in df:
            continue
        invalid = df[c].gt(df[a])
        flag(invalid, "completions exceed attempts: pair excluded", c, "error")
        df.loc[invalid, [a, c]] = np.nan
        computed = df[c].div(df[a].where(df[a].gt(0)))
        if r in df:
            mismatch = df[r].notna() & (computed.isna() | df[r].sub(computed).abs().gt(1e-6))
            flag(mismatch, "stored rate differs from valid pair; recomputed", r)
        df[r] = computed
    for parts, total in [(["shots_inside_box", "shots_outside_box"], "shots"),
                         (["shots_on_target", "shots_off_target", "shots_blocked"], "shots")]:
        if set(parts + [total]) <= set(df):
            comparable = df[parts + [total]].notna().all(axis=1)
            flag(comparable & df[parts].sum(axis=1).ne(df[total]),
                 f"{'+'.join(parts)} differs from {total}; inspect sources", total)
    df["position_group"] = df.position.map(POSITIONS).fillna("Unknown")
    df["month"] = df.date.dt.strftime("%Y-%m")
    evidence_columns = [m for m in RAW_METRICS if m in df and m != "minutes_played"]
    evidence = df[evidence_columns].notna().any(axis=1)
    inferred = df.starter.fillna(False) | df.minutes_played.gt(0) | evidence
    if "appeared" in df:
        explicit = boolean(df.appeared)
        flag(explicit.eq(False) & inferred, "appearance flag conflicts with stats; evidence retained", "appeared")
        inferred |= explicit.fillna(False)
    df["appearance"] = inferred
    df["appearance_basis"] = np.select(
        [df.starter.fillna(False), df.minutes_played.gt(0), inferred],
        ["starter", "positive minutes", "appearance flag or recorded stats"],
        default="no appearance evidence")
    flag(df.appearance & ~df.minutes_played.gt(0),
         "appearance lacks positive minutes: excluded from per-90", "minutes_played")
    excluded = duplicated | missing_context | dates.isna()
    clean = df.loc[~excluded].copy()
    for (match_id, team), squad in clean.groupby(["match_id", "team"]):
        if squad.starter.sum() != 11:
            flag(df.match_id.eq(match_id) & df.team.eq(team), "lineup does not have 11 starters", "starter")
    team_counts = clean.groupby("match_id").team.nunique()
    flag(df.match_id.isin(team_counts[team_counts.ne(2)].index),
         "match does not have two teams (may be filtered input)", "team")
    columns = KEY + ["player_name", "team", "check", "metric", "severity", "value"]
    return clean, pd.concat(issues, ignore_index=True) if issues else pd.DataFrame(columns=columns)


def metric_catalog(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for metric in RAW_METRICS + RATE_METRICS:
        stem = next((s for s in PAIRS if metric.startswith(s + "_")), None)
        category = SCALARS[metric][0] if metric in SCALARS else PAIRS[stem][0] if stem else "shooting"
        unit = SCALARS[metric][-1] if metric in SCALARS else "fraction" if metric in RATE_METRICS else "count"
        perspective = "activity / role evidence" if metric in ACTIVITY else (
            "execution / performance" if metric in RATE_METRICS or metric in OUTCOMES else "context / supporting statistic")
        rows.append(dict(metric=metric, category=category, unit=unit,
                         perspective=perspective, column_present=metric in df))
    return pd.DataFrame(rows)


def coverage_table(df: pd.DataFrame, metrics: list[str], groups=()) -> pd.DataFrame:
    """Goalkeeping coverage uses goalkeeper appearances; other metrics use all.

    Rate denominators require a complete pair and positive attempts. For other
    metrics a zero is an observed value, not a missing value.
    """
    rows = []
    partitions = df.groupby(list(groups), dropna=False) if groups else [((), df)]
    for key, part in partitions:
        key = key if isinstance(key, tuple) else (key,)
        context = dict(zip(groups, key))
        for metric in metrics:
            eligible = part
            if metric in SCALARS and SCALARS[metric][0] == "goalkeeping":
                eligible = part.loc[part.position_group.eq("Goalkeeper")]
            if metric in RATE_METRICS:
                stem = metric.removesuffix("_completion_rate")
                a, c = stem + "_attempted", stem + "_completed"
                if a in part and c in part:
                    eligible = part.loc[part[a].gt(0) & part[c].notna()]
                else:
                    eligible = part.iloc[:0]
            values = eligible[metric] if metric in eligible else pd.Series(np.nan, index=eligible.index)
            n, observed = len(eligible), int(values.notna().sum())
            rows.append({**context, "metric": metric, "eligible_appearances": n,
                         "observed": observed, "missing": n - observed,
                         "coverage": observed / n if n else np.nan,
                         "zero_share_of_observed": values.eq(0).sum() / observed if observed else np.nan})
    return pd.DataFrame(rows)


def player_summaries(appearances: pd.DataFrame, min_minutes: float,
                     min_coverage: float, min_attempts: int):
    """Aggregate within player, season and team; retain transfer stints separately.

    Per-90 denominators include ONLY positive minutes with the metric observed.
    Pair rates are sums of completed / sums attempted on the SAME complete rows.
    No averaging per-match percentages; no sum of an all-missing series becomes 0.
    """
    exposure, metric_rows, pair_rows = [], [], []
    for key, group in appearances.groupby(PLAYER, dropna=False, sort=True):
        context = dict(zip(PLAYER, key))
        modes = group.position_group.mode()
        context["position_group"] = modes.iloc[0] if len(modes) == 1 else "Mixed"
        timed = group.minutes_played.gt(0)
        total_minutes = group.loc[timed, "minutes_played"].sum(min_count=1)
        exposure.append({**context, "appearances": len(group), "starts": int(group.starter.sum()),
                         "known_minutes": total_minutes, "appearances_without_positive_minutes": int((~timed).sum())})
        for metric in dict.fromkeys(PER90 + CORRELATION_METRICS):
            if metric not in group:
                continue
            valid = timed & group[metric].notna()
            minutes = group.loc[valid, "minutes_played"].sum(min_count=1)
            value = group.loc[valid, metric].sum(min_count=1)
            coverage = minutes / total_minutes if total_minutes > 0 else np.nan
            eligible = bool(minutes >= min_minutes and coverage >= min_coverage)
            metric_rows.append({**context, "metric": metric,
                                "observed_appearances": int(group[metric].notna().sum()),
                                "observed_total": group[metric].sum(min_count=1),
                                "timed_total": value, "observed_minutes": minutes,
                                "minute_coverage": coverage,
                                "per90": 90 * value / minutes if minutes > 0 else np.nan,
                                "eligible_comparison": eligible})
        for stem in PAIRS:
            a, c = stem + "_attempted", stem + "_completed"
            if a not in group or c not in group:
                continue
            complete = group[a].notna() & group[c].notna()
            attempted = group.loc[complete, a].sum(min_count=1)
            completed = group.loc[complete, c].sum(min_count=1)
            pair_minutes = group.loc[complete & timed, "minutes_played"].sum(min_count=1)
            coverage = pair_minutes / total_minutes if total_minutes > 0 else np.nan
            pair_rows.append({**context, "action": stem, "complete_pair_appearances": int(complete.sum()),
                              "paired_attempts": attempted, "paired_completions": completed,
                              "observed_minutes": pair_minutes, "minute_coverage": coverage,
                              "completion_rate": completed / attempted if attempted > 0 else np.nan,
                              "eligible_comparison": bool(pair_minutes >= min_minutes and
                                                          coverage >= min_coverage and attempted >= min_attempts)})
    return pd.DataFrame(exposure), pd.DataFrame(metric_rows), pd.DataFrame(pair_rows)


def source_analysis(folder: Path, appearances: pd.DataFrame):
    """Read only provenance columns needed, in chunks, to bound memory use."""
    keys = appearances[KEY + ["season"]].drop_duplicates(KEY)
    chunks = []
    source_path = folder / "provenance.csv"
    if source_path.exists() and source_path.stat().st_size > 1:
        for chunk in pd.read_csv(source_path, usecols=KEY + ["metric", "provider", "value"], chunksize=100_000):
            chunk = chunk.merge(keys, on=KEY, how="inner", validate="many_to_one")
            observed = chunk.loc[chunk.value.notna() & chunk.provider.notna()]
            chunks.append(observed.groupby(["season", "metric", "provider"]).size().rename("observations"))
    shares = pd.concat(chunks).groupby(level=[0, 1, 2]).sum().reset_index() if chunks else pd.DataFrame()
    if not shares.empty:
        shares["share_of_observed"] = shares.observations / shares.groupby(["season", "metric"]).observations.transform("sum")
    differences = read_optional(folder / "source_differences.csv")
    if not differences.empty:
        differences = differences.rename(columns={"chosen_source": "selected_provider", "chosen_value": "selected_value",
                                                   "other_source": "other_provider"})
        differences = differences.merge(keys[KEY], on=KEY, how="inner", validate="many_to_one")
        differences["absolute_difference"] = (differences.selected_value - differences.other_value).abs()
        # Two-decimal expected metrics can differ by <= .005 solely by rounding.
        # This is a tolerance flag, not proof that a disagreement is harmless.
        expected = differences.metric.isin(["xg", "xgot", "xa", "non_penalty_xg", "goals_prevented", "xgot_faced"])
        differences["within_rounding_tolerance"] = expected & differences.absolute_difference.le(0.005001)
        summary = differences.groupby(["metric", "selected_provider", "other_provider"], dropna=False).agg(
            differing_observations=("absolute_difference", "size"),
            affected_matches=("match_id", "nunique"),
            median_absolute_difference=("absolute_difference", "median"),
            max_absolute_difference=("absolute_difference", "max"),
            within_rounding_tolerance=("within_rounding_tolerance", "sum")).reset_index()
    else:
        summary = pd.DataFrame()
    return shares, differences, summary


def correlation_table(metrics: pd.DataFrame, min_players=20) -> pd.DataFrame:
    """Exploratory Spearman associations within season and broad position.

    Pairwise n is exported. These are repeated-player observational summaries,
    not independent experimental observations, significance tests or causal claims.
    """
    columns = ["season", "position_group", "metric_x", "metric_y", "player_team_stints", "spearman_r"]
    if metrics.empty:
        return pd.DataFrame(columns=columns)
    eligible = metrics.loc[metrics.eligible_comparison & metrics.metric.isin(CORRELATION_METRICS)]
    rows = []
    for (season, position), group in eligible.groupby(["season", "position_group"]):
        wide = group.pivot(index=["player_id", "team"], columns="metric", values="per90")
        names = sorted(wide.columns)
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                pair = wide[[left, right]].dropna()
                coefficient = np.nan
                if len(pair) >= min_players and pair[left].nunique() > 1 and pair[right].nunique() > 1:
                    coefficient = pair.rank().corr().iloc[0, 1]
                rows.append(dict(season=season, position_group=position, metric_x=left,
                                 metric_y=right, player_team_stints=len(pair), spearman_r=coefficient))
    return pd.DataFrame(rows, columns=columns)


def conditional_coverage(appearances: pd.DataFrame) -> pd.DataFrame:
    """Check whether missing expected values coincide with no recorded shots.

    This diagnoses selection in observed-only summaries without filling zeros.
    """
    frames = []
    for conditioning, targets in [("shots", ["xg", "non_penalty_xg"]),
                                   ("shots_on_target", ["xgot"])]:
        if conditioning not in appearances:
            continue
        work = appearances.copy()
        work["activity_bucket"] = np.select(
            [work[conditioning].eq(0), work[conditioning].gt(0)],
            ["zero recorded", "positive recorded"], default="unknown")
        frame = coverage_table(work, [m for m in targets if m in work], ["season", "activity_bucket"])
        frame["conditioning_metric"] = conditioning
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["season", "activity_bucket", "metric", "eligible_appearances", "observed",
                 "missing", "coverage", "zero_share_of_observed", "conditioning_metric"])


def make_figures(appearances, coverage, by_month, metrics, pairs):
    figures = []
    overall = coverage.loc[~coverage.metric.isin(RATE_METRICS)].copy()
    overall = overall.sort_values("coverage").groupby("season", group_keys=False).head(25)
    overall["metric_label"] = overall.metric.str.replace("_", " ")
    figures.append(px.bar(overall, x="coverage", y="metric_label", orientation="h", facet_col="season",
                          hover_data=["observed", "eligible_appearances", "zero_share_of_observed"],
                          title="25 least available metrics per season — eligible appearances",
                          labels={"metric_label": "Metric", "coverage": "Share observed"}, height=750))
    figures[-1].update_xaxes(tickformat=".0%", range=[0, 1])
    figures[-1].update_yaxes(autorange="reversed")
    tracked = ["through_balls_attempted", "xg", "passes_into_final_third", "top_speed_kmh",
               "line_breaking_passes", "progressive_carries"]
    figures.append(px.line(by_month.loc[by_month.metric.isin(tracked)], x="month", y="coverage",
                           color="metric", line_dash="season", markers=True,
                           hover_data=["observed", "eligible_appearances"],
                           title="Availability over time — gaps may reflect collection or provider coverage"))
    figures[-1].update_yaxes(tickformat=".0%", range=[0, 1.02])
    figures.append(px.histogram(appearances, x="minutes_played", color="position_group", nbins=30,
                                facet_col="season", title="Minutes per appearance (missing minutes omitted)",
                                labels={"minutes_played": "Minutes played"}))
    distribution_metrics = [m for m in ["shots", "xg", "xa", "passes_attempted", "dribbles_attempted", "tackles"] if m in appearances]
    long = appearances.melt(id_vars=["season", "position_group"], value_vars=distribution_metrics,
                            var_name="metric", value_name="value").dropna(subset=["value"])
    if not long.empty:
        fig = px.histogram(long, x="value", facet_col="metric", facet_col_wrap=3,
                           color="season", barmode="overlay", opacity=.6, nbins=35,
                           title="Observed match-stat distributions — zeros retained, missing values omitted")
        fig.update_xaxes(matches=None)
        fig.update_yaxes(matches=None)
        figures.append(fig)
    if not metrics.empty:
        chosen = metrics.loc[metrics.eligible_comparison & metrics.metric.isin(
            ["passes_attempted", "dribbles_attempted", "shots", "tackles", "through_balls_attempted", "xg"])]
        if not chosen.empty:
            fig = px.box(chosen, x="position_group", y="per90", color="season", facet_col="metric",
                         facet_col_wrap=3, points=False,
                         title="Activity and xG per 90 observed minutes — eligible player-season-team summaries")
            fig.update_yaxes(matches=None)
            figures.append(fig)
    if not pairs.empty:
        selected = pairs.loc[pairs.eligible_comparison & pairs.action.isin(["passes", "dribbles"])]
        if not selected.empty:
            plot = selected.pivot(index=PLAYER + ["position_group"], columns="action",
                                  values=["completion_rate", "paired_attempts"])
            plot.columns = [f"{a}_{b}" for a, b in plot.columns]
            axes = ["completion_rate_passes", "completion_rate_dribbles"]
            if set(axes) <= set(plot):
                plot = plot.dropna(subset=axes).reset_index()
                if not plot.empty:
                    fig = px.scatter(plot, x=axes[0], y=axes[1], color="position_group", symbol="season",
                                     hover_name="player_name", hover_data=["team", "paired_attempts_passes", "paired_attempts_dribbles"],
                                     title="Pass and dribble completion — ratios of paired totals, with attempt thresholds")
                    fig.update_xaxes(tickformat=".0%")
                    fig.update_yaxes(tickformat=".0%")
                    figures.append(fig)
    for fig in figures:
        fig.update_layout(template="plotly_white", font=dict(family="Arial", size=12), margin=dict(t=85))
    return figures


def write_report(output: Path, source: Path, notes: list[str], tables: dict,
                 figures: list, settings: dict):
    output.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_csv(output / f"{name}.csv", index=False)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "input_csv": str(source),
                "input_bytes": source.stat().st_size, "input_modified_ns": source.stat().st_mtime_ns,
                "settings": settings, "notes": notes,
                "table_rows": {name: len(frame) for name, frame in tables.items()}}
    (output / "analysis_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    overview = tables["scope"].to_html(index=False, border=0)
    issues = tables["quality_issues"]
    checks = issues.groupby(["severity", "check"]).size().rename("flagged_rows").reset_index()
    quality_html = checks.to_html(index=False, border=0) if len(checks) else "<p>No audit flags.</p>"
    plots = [fig.to_html(full_html=False, include_plotlyjs=True if i == 0 else False,
                         config={"displaylogo": False, "responsive": True}) for i, fig in enumerate(figures)]
    links = "".join(f'<li><a href="{escape(name)}.csv">{escape(name.replace("_", " "))}</a> ({len(frame):,} rows)</li>' for name, frame in tables.items())
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Soccer player data exploration</title><style>
    body{{font:16px/1.55 Arial,sans-serif;color:#213047;max-width:1400px;margin:40px auto;padding:0 24px}}
    h1,h2{{line-height:1.2}} table{{border-collapse:collapse;font-size:14px;display:block;overflow:auto}}
    th,td{{padding:9px 13px;border-bottom:1px solid #dce3ed;text-align:left}} th{{background:#eef3f8}}
    .chart{{margin:35px 0}} li{{margin:8px 0}} a{{color:#1261a0}}
    </style></head><body><h1>Soccer player data exploration</h1>
    <p>Source: {escape(str(source))}</p><h2>Scope and findings</h2>{overview}
    <ul>{''.join('<li>' + escape(n) + '</li>' for n in notes)}</ul>
    <h2>Data quality</h2>{quality_html}<h2>Exploratory charts</h2>
    {''.join('<div class="chart">' + p + '</div>' for p in plots)}
    <h2>How to interpret this analysis</h2><ul>
    <li>The input grain is a player in a match lineup. Only appearances enter statistical summaries.
    A starter, positive minutes, an explicit appearance flag or recorded match statistics provides appearance evidence.
    Rows with no evidence are excluded; that does not prove a substitute never entered the pitch.</li>
    <li>Missing is unknown, not zero. Zero attempts have no defined completion rate.
    Goalkeeping availability uses goalkeepers only. Rate availability uses complete pairs with positive attempts.</li>
    <li>Per-90 values use minutes from rows where that particular metric is observed.
    Aggregate completion uses complete attempt/completion pairs. Both export their coverage and exposure.</li>
    <li>Observed-only expected-goal rates can be biased upward when zero-shot appearances omit xG.
    Check conditional_coverage before comparing xG or xGOT. No unrecorded zero is inferred here.</li>
    <li>Comparative plots require at least {settings['min_minutes']:g} observed minutes,
    {settings['min_coverage']:.0%} minute coverage, and {settings['min_attempts']} attempts for completion rates.
    Thresholds limit unstable comparisons; they do not establish reliability.</li>
    <li>Teams and seasons are kept separate, including transfer stints. Position groups are broad labels,
    not inferred roles. Activity and execution are descriptive and depend on team tactics and opponents.</li>
    <li>Missingness can be systematic. Coverage by month, position and collection status is exported.
    Provider shares describe selected values; the differences file contains only disagreements, so it cannot
    supply an overall disagreement rate. Expected-value differences within .005 are flagged as compatible
    with two-decimal rounding, not automatically dismissed.</li>
    <li>Correlations are exploratory Spearman associations within season and position, with pairwise sample
    counts and a minimum of 20 player-team stints. They are not causal or adjusted for team/opponent effects.
    Related metrics can be mathematically coupled, and repeated appearances are not independent samples.</li>
    <li>Outliers are review candidates, not automatic errors. No traits, map interpretation, imputation,
    statistical significance claims or composite rankings are produced.</li></ul>
    <h2>Analysis tables</h2><ul>{links}</ul></body></html>"""
    (output / "report.html").write_text(html, encoding="utf-8")


def run_eda(data: Path = DEFAULT_DATA, output: Path | None = None, *, team=None,
            season=None, min_minutes=450, min_coverage=.8, min_attempts=20):
    """Run the complete analysis and return its tables for interactive use."""
    if min_minutes <= 0 or not 0 <= min_coverage <= 1 or min_attempts < 1:
        raise ValueError("Require positive minutes/attempts and coverage between 0 and 1.")
    raw, source, metadata = load_data(Path(data).expanduser().resolve())
    output = Path(output).expanduser().resolve() if output else PROJECT / "outputs" / "eda" / source.parent.name
    # Protect all source CSVs, even when a caller chooses an unsafe output folder.
    if output == source.parent:
        raise ValueError("Choose an output folder different from the source data folder.")
    clean, issues = prepare_data(raw)
    collection = read_optional(source.parent / "collection.csv")
    if not collection.empty:
        if collection.match_id.duplicated().any():
            raise ValueError("Duplicate match IDs in collection status file.")
        clean = clean.merge(collection[["match_id", "status"]].rename(columns={"status": "collection_status"}),
                            on="match_id", how="left", validate="many_to_one")
    else:
        clean["collection_status"] = "not recorded"
    clean["collection_status"] = clean.collection_status.fillna("not recorded")
    if team:
        available = clean.team.dropna().unique()
        matches = [t for t in available if t.casefold() == team.casefold()]
        if not matches:
            matches = [t for t in available if team.casefold() in t.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Team must match one name; matches: {matches}; available: {sorted(available)}")
        clean = clean.loc[clean.team.eq(matches[0])].copy()
    if season:
        clean = clean.loc[clean.season.eq(season)].copy()
    if clean.empty or not clean.appearance.any():
        raise ValueError("No appearances available for the selected dataset/filters.")
    appearances = clean.loc[clean.appearance].copy()
    selected_matches = clean.match_id.unique()
    collection = collection.loc[collection.match_id.isin(selected_matches)] if not collection.empty else collection
    metrics = [m for m in RAW_METRICS + RATE_METRICS if m in clean]
    scope = clean.groupby("season").agg(
        matches=("match_id", "nunique"), teams=("team", "nunique"), players_in_squads=("player_id", "nunique"),
        squad_rows=("player_id", "size"), appearances=("appearance", "sum"),
        first_date=("date", "min"), last_date=("date", "max")).reset_index()
    scope = scope.merge(appearances.groupby("season").player_id.nunique().rename("players_with_appearances"), on="season")
    exposure, player_metrics, player_pairs = player_summaries(appearances, min_minutes, min_coverage, min_attempts)
    coverage = coverage_table(appearances, metrics, ["season"])
    by_month = coverage_table(appearances, metrics, ["season", "month"])
    source_shares, differences, difference_summary = source_analysis(source.parent, appearances)
    conditional = conditional_coverage(appearances)
    description = appearances.groupby("season")[metrics].describe(percentiles=[.05, .25, .5, .75, .95]).stack(level=0, future_stack=True).reset_index()
    description = description.rename(columns={description.columns[1]: "metric"})
    outlier_rows = []
    # Within-position/season IQR flags; sparse binary/count fields with IQR=0 are skipped.
    for (s, position), group in appearances.groupby(["season", "position_group"]):
        for metric in [m for m in PER90 if m in group]:
            values = group[metric].dropna()
            if len(values) < 20:
                continue
            q1, q3 = values.quantile([.25, .75])
            if q3 <= q1:
                continue
            lower, upper = q1 - 3 * (q3 - q1), q3 + 3 * (q3 - q1)
            mask = group[metric].lt(lower) | group[metric].gt(upper)
            flagged = group.loc[mask, KEY + ["date", "player_name", "team", "minutes_played"]].copy()
            flagged["season"], flagged["position_group"] = s, position
            flagged["metric"], flagged["value"] = metric, group.loc[mask, metric]
            flagged["lower_fence"], flagged["upper_fence"] = lower, upper
            outlier_rows.append(flagged)
    tables = {
        "scope": scope, "metric_catalog": metric_catalog(clean), "quality_issues": issues,
        "appearance_summary": clean.groupby(["season", "appearance_basis"]).size().rename("rows").reset_index(),
        "collection_status": collection, "coverage": coverage,
        "coverage_by_position": coverage_table(appearances, metrics, ["season", "position_group"]),
        "coverage_by_team": coverage_table(appearances, metrics, ["season", "team"]),
        "coverage_by_month": by_month,
        "conditional_coverage": conditional,
        "coverage_by_collection_status": coverage_table(appearances, metrics, ["season", "collection_status"]),
        "metric_distributions": description, "player_exposure": exposure,
        "player_metrics": player_metrics, "player_action_pairs": player_pairs,
        "source_shares": source_shares, "source_differences": differences,
        "source_difference_summary": difference_summary,
        "correlations": correlation_table(player_metrics),
        "outlier_candidates": pd.concat(outlier_rows, ignore_index=True) if outlier_rows else pd.DataFrame(),
    }
    absent = coverage.loc[coverage.observed.eq(0) & coverage.eligible_appearances.gt(0) & ~coverage.metric.isin(RATE_METRICS)]
    notes = [f"Analyzed {len(appearances):,} appearances in {clean.match_id.nunique():,} matches; "
             f"{len(clean) - len(appearances):,} squad rows had no appearance evidence.",
             f"Data-quality audit flagged {len(issues):,} row/check combinations across the input CSV before filters."]
    if metadata:
        notes.append(f"Saved collection summary for the entire source: {metadata.get('complete', '?')} complete matches, "
                     f"{metadata.get('partial', '?')} partial, {metadata.get('failed_or_pending', '?')} failed or pending. "
                     "Complete refers to provider collection, not availability of every statistic.")
    if not absent.empty:
        notes.append("Metrics entirely unobserved in at least one selected season: " + ", ".join(sorted(absent.metric.unique())) + ".")
    for _, row in conditional.loc[conditional.metric.eq("xg") & conditional.activity_bucket.eq("zero recorded")].iterrows():
        if row.eligible_appearances:
            notes.append(f"{row.season}: xG is missing in {int(row['missing']):,} of "
                         f"{int(row.eligible_appearances):,} zero-shot appearances. Observed-only xG summaries "
                         "can therefore overrepresent shooting appearances; do not read these rates as complete-season xG/90.")
    if not collection.empty and collection.status.ne("complete").any():
        errors = collection.loc[collection.status.ne("complete"), "error"].dropna().value_counts()
        if not errors.empty:
            notes.append(f"Most frequent collection error ({errors.iloc[0]} matches): {errors.index[0]}")
    if team or clean.match_id.nunique() < 30:
        notes.append("This is a restricted sample; opponent selection, unequal appearances and short exposure limit generalization.")
    settings = dict(team=team, season=season, min_minutes=min_minutes, min_coverage=min_coverage, min_attempts=min_attempts)
    figures = make_figures(appearances, coverage, by_month, player_metrics, player_pairs)
    write_report(output, source, notes, tables, figures, settings)
    print(scope.to_string(index=False))
    for note in notes:
        print(f"\n{note}")
    print(f"\nReport: {output / 'report.html'}\nAnalysis tables: {output}")
    return tables


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="Saved collection directory or player CSV")
    parser.add_argument("--output", type=Path, help="Report directory (default: outputs/eda/<dataset>)")
    parser.add_argument("--team", help="One team name, case-insensitive; a unique substring is accepted")
    parser.add_argument("--season", help="Exact season label in the CSV")
    parser.add_argument("--min-minutes", type=float, default=450, help="Observed minutes required for comparative plots")
    parser.add_argument("--min-coverage", type=float, default=.8, help="Required proportion of known minutes with a metric")
    parser.add_argument("--min-attempts", type=int, default=20, help="Paired attempts required for completion-rate plots")
    args = parser.parse_args()
    try:
        run_eda(**vars(args))
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
