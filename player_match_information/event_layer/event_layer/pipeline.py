"""Pure transformations and a file-based orchestration boundary.

The module deliberately has no dependency on the collection package.  Its input
is an immutable CSV snapshot and every output is a derived table.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from pitch_zones import channel as pitch_channel, zone_number


REQUIRED_COLUMNS = {
    "match_id", "event_index", "event_id", "period", "minute", "team_id",
    "player_id", "event_type", "outcome", "x", "y", "end_x", "end_y",
}
ADMINISTRATIVE = {"Start", "End", "FormationSet", "FormationChange", "SubstitutionOff", "SubstitutionOn"}
SHOT_TYPES = {"Goal", "MissedShots", "SavedShot", "ShotOnPost", "ChanceMissed"}
RESTART_QUALIFIERS = {"CornerTaken", "FreekickTaken", "ThrowIn", "GoalKick", "Penalty"}
GAIN_TYPES = {"BallRecovery", "Interception", "KeeperPickup", "Claim", "Punch", "Save", "KeeperSweeper"}
DEFENSIVE_TYPES = {"Tackle", "Challenge", "Interception", "Clearance", "BlockedPass"}
ON_BALL_TYPES = {
    "Pass", "OffsidePass", "BallTouch", "TakeOn", "Dispossessed", "BallRecovery",
    "Interception", "Tackle", "Clearance", "KeeperPickup", "KeeperSweeper", *SHOT_TYPES,
}
POSSESSION_END_TYPES = {"OffsidePass", "OffsideGiven", "Dispossessed", *SHOT_TYPES}


@dataclass(frozen=True)
class PipelineConfig:
    pitch_length_m: float = 105.0
    pitch_width_m: float = 68.0
    receiver_max_seconds: float = 8.0
    receiver_max_events: int = 8
    receiver_max_distance_m: float = 15.0
    carry_min_distance_m: float = 3.0
    carry_max_distance_m: float = 60.0
    carry_max_seconds: float = 10.0


@dataclass
class PipelineResult:
    events_enriched: pd.DataFrame
    possessions: pd.DataFrame
    player_match_features: pd.DataFrame
    player_match_profiles: list[dict]


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None or pd.isna(value):
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _qualifier_map(value) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in _json_list(value):
        if not isinstance(item, dict):
            continue
        type_value = item.get("type")
        name = type_value.get("displayName") if isinstance(type_value, dict) else None
        if name:
            result[name] = item.get("value", True)
    return result


def _third(x: float) -> str | None:
    if pd.isna(x):
        return None
    return "defensive" if x < 100 / 3 else "middle" if x < 200 / 3 else "attacking"


def _channel(y: float) -> str | None:
    if pd.isna(y):
        return None
    # WhoScored's attacking-view frame: small y is the right side.
    return pitch_channel(y, center="center")


def _zone(x: float, y: float) -> str | None:
    third, channel = _third(x), _channel(y)
    return f"{third}_{channel}" if third and channel else None


def _event_family(event_type: str) -> str:
    if event_type in {"Pass", "OffsidePass"}:
        return "pass"
    if event_type in SHOT_TYPES:
        return "shot"
    if event_type in {"TakeOn", "Aerial", "Challenge", "Tackle", "Foul", "Dispossessed"}:
        return "duel"
    if event_type in GAIN_TYPES:
        return "recovery"
    if event_type in DEFENSIVE_TYPES:
        return "defensive"
    if event_type in ADMINISTRATIVE:
        return "administrative"
    return "other"


def _effective_outcome(event_type: str, outcome: str) -> str:
    if event_type in {"OffsidePass", "OffsideGiven"}:
        return "possession_lost_offside"
    if event_type == "Goal":
        return "goal"
    if event_type in SHOT_TYPES:
        return "shot_no_goal"
    return "success" if outcome == "Successful" else "failure"


def _validate(raw: pd.DataFrame) -> None:
    missing = REQUIRED_COLUMNS - set(raw.columns)
    if missing:
        raise ValueError(f"Input is missing required columns: {', '.join(sorted(missing))}")
    if raw.empty:
        raise ValueError("Input event stream is empty")
    duplicates = raw.duplicated(["match_id", "event_index"])
    if duplicates.any():
        raise ValueError("match_id + event_index must uniquely identify input rows")


def enrich_events(raw: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    """Normalize geometry and semantics without changing the raw columns."""
    _validate(raw)
    events = raw.copy()
    numeric = ["event_index", "event_id", "period", "minute", "second", "expanded_minute",
               "team_id", "player_id", "x", "y", "end_x", "end_y", "pass_length", "pass_angle"]
    for column in numeric:
        if column in events:
            events[column] = pd.to_numeric(events[column], errors="coerce")
    for column in ("x", "y", "end_x", "end_y"):
        events.loc[~events[column].between(0, 100), column] = math.nan

    events = events.sort_values(["match_id", "period", "event_index"], kind="stable").reset_index(drop=True)
    fallback_key = events["match_id"].astype("string") + ":index:" + events["event_index"].astype("Int64").astype("string")
    if "provider_event_id" in events:
        provider = pd.to_numeric(events["provider_event_id"], errors="coerce").astype("Int64").astype("string")
        events["event_uid"] = (events["match_id"].astype("string") + ":provider:" + provider).where(provider.notna(), fallback_key)
    else:
        events["event_uid"] = fallback_key
    # Provider IDs are nearly unique, but real exports contain occasional
    # duplicates. Preserve the readable provider key and disambiguate only those.
    duplicate_uid = events["event_uid"].duplicated(keep=False)
    events.loc[duplicate_uid, "event_uid"] = (
        events.loc[duplicate_uid, "event_uid"] + ":index:"
        + events.loc[duplicate_uid, "event_index"].astype("Int64").astype("string")
    )
    events["match_second"] = events.get("expanded_minute", events["minute"]).fillna(events["minute"]) * 60 + events["second"].fillna(0)

    sx, sy = events["x"] * config.pitch_length_m / 100, events["y"] * config.pitch_width_m / 100
    ex, ey = events["end_x"] * config.pitch_length_m / 100, events["end_y"] * config.pitch_width_m / 100
    events["start_x_m"], events["start_y_m"] = sx, sy
    events["end_x_m"], events["end_y_m"] = ex, ey
    events["dx_m"], events["dy_m"] = ex - sx, ey - sy
    events["forward_distance_m"] = events["dx_m"]
    events["calculated_length_m"] = (events["dx_m"] ** 2 + events["dy_m"] ** 2) ** 0.5
    events["distance_to_goal_start_m"] = ((config.pitch_length_m - sx) ** 2 + (config.pitch_width_m / 2 - sy) ** 2) ** 0.5
    events["distance_to_goal_end_m"] = ((config.pitch_length_m - ex) ** 2 + (config.pitch_width_m / 2 - ey) ** 2) ** 0.5
    events["goal_distance_reduction_m"] = events["distance_to_goal_start_m"] - events["distance_to_goal_end_m"]
    events["start_third"] = events["x"].map(_third)
    events["end_third"] = events["end_x"].map(_third)
    events["start_channel"] = events["y"].map(_channel)
    events["end_channel"] = events["end_y"].map(_channel)
    events["start_zone"] = [_zone(x, y) for x, y in zip(events["x"], events["y"])]
    events["end_zone"] = [_zone(x, y) for x, y in zip(events["end_x"], events["end_y"])]
    events["start_zone_number"] = [zone_number(x, y) if pd.notna(x) and pd.notna(y) else pd.NA
                                   for x, y in zip(events["x"], events["y"])]
    events["end_zone_number"] = [zone_number(x, y) if pd.notna(x) and pd.notna(y) else pd.NA
                                 for x, y in zip(events["end_x"], events["end_y"])]
    events["event_family"] = events["event_type"].map(_event_family)
    events["event_subtype"] = events["event_type"]
    events["effective_outcome"] = [_effective_outcome(t, o) for t, o in zip(events["event_type"], events["outcome"])]
    events["effective_success"] = events["effective_outcome"].isin(["success", "goal"])

    if "qualifiers_json" in events:
        qualifier_maps = events["qualifiers_json"].map(_qualifier_map)
    else:
        qualifier_maps = pd.Series([{} for _ in range(len(events))], index=events.index)
    events["opposite_related_event_id"] = pd.to_numeric(
        qualifier_maps.map(lambda q: q.get("OppositeRelatedEvent")), errors="coerce"
    ).astype("Int64")
    events["open_play_or_restart"] = qualifier_maps.map(
        lambda q: "restart" if RESTART_QUALIFIERS.intersection(q) else "open_play"
    )
    events["is_administrative"] = events["event_type"].isin(ADMINISTRATIVE)
    events["is_progressive"] = (
        events["event_type"].isin(["Pass", "OffsidePass"])
        & events["calculated_length_m"].ge(10)
        & (events["goal_distance_reduction_m"] / events["distance_to_goal_start_m"]).ge(
            events["x"].lt(50).map({True: 0.25, False: 0.15})
        )
    )
    events["is_final_third_entry"] = events["x"].lt(200 / 3) & events["end_x"].ge(200 / 3)
    events["is_box_entry"] = (~(events["x"].ge(84.3) & events["y"].between(20.35, 79.65))
                              & events["end_x"].ge(84.3) & events["end_y"].between(20.35, 79.65))
    return events


def pair_opponent_events(events: pd.DataFrame) -> pd.DataFrame:
    """Resolve provider event-id links in the opposing team's coordinate frame."""
    events = events.copy()
    events["paired_opponent_event_uid"] = pd.NA
    events["is_paired_duplicate"] = False
    candidates: dict[tuple[object, int], list[int]] = {}
    for idx, row in events.loc[events["event_id"].notna()].iterrows():
        candidates.setdefault((row["match_id"], int(row["event_id"])), []).append(idx)
    for idx, row in events.loc[events["opposite_related_event_id"].notna()].iterrows():
        choices = candidates.get((row["match_id"], int(row["opposite_related_event_id"])), [])
        choices = [j for j in choices if events.at[j, "team_id"] != row["team_id"] and abs(events.at[j, "event_index"] - row["event_index"]) <= 4]
        if not choices:
            continue
        other = min(choices, key=lambda j: abs(events.at[j, "event_index"] - row["event_index"]))
        events.at[idx, "paired_opponent_event_uid"] = events.at[other, "event_uid"]
        events.at[idx, "is_paired_duplicate"] = events.at[idx, "event_index"] > events.at[other, "event_index"]
    return events


def assign_possessions(events: pd.DataFrame) -> pd.DataFrame:
    """Assign deterministic, conservative possessions to the ordered stream."""
    events = events.copy()
    events["possession_id"] = pd.Series(pd.NA, index=events.index, dtype="Int64")
    events["sequence_index"] = pd.Series(pd.NA, index=events.index, dtype="Int64")
    for (_, _), indices in events.groupby(["match_id", "period"], sort=False, dropna=False).groups.items():
        current_team = None
        possession = 0
        sequence = 0
        end_pending = False
        for idx in indices:
            row = events.loc[idx]
            if row["is_administrative"]:
                continue
            if row["is_paired_duplicate"]:
                events.at[idx, "possession_id"] = possession if possession else pd.NA
                events.at[idx, "sequence_index"] = sequence if possession else pd.NA
                continue
            team = row["team_id"]
            starts_control = row["event_type"] in GAIN_TYPES or row["event_type"] in {"Pass", "TakeOn", "BallTouch", "Tackle"}
            if current_team is None or end_pending or (team != current_team and starts_control):
                possession += 1
                sequence = 0
                current_team = team
                end_pending = False
            sequence += 1
            events.at[idx, "possession_id"] = possession
            events.at[idx, "sequence_index"] = sequence
            unsuccessful_pass = row["event_type"] == "Pass" and row["outcome"] != "Successful"
            if row["event_type"] in POSSESSION_END_TYPES or unsuccessful_pass:
                end_pending = True
        # IDs are unique within a match, not just a period.
        period_value = int(events.loc[indices[0], "period"]) if pd.notna(events.loc[indices[0], "period"]) else 0
        mask = events.index.isin(indices) & events["possession_id"].notna()
        events.loc[mask, "possession_id"] += period_value * 1_000_000
    return events


def infer_receivers_and_carries(events: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    events = events.copy()
    events["inferred_receiver_id"] = pd.Series(pd.NA, index=events.index, dtype="Float64")
    events["inferred_receiver_name"] = pd.NA
    events["receiver_event_uid"] = pd.NA
    events["receiver_confidence"] = pd.NA
    events["receiver_distance_m"] = math.nan
    events["inferred_carry_distance_m"] = math.nan
    events["carry_origin_event_uid"] = pd.NA

    for _, indices_obj in events.groupby("match_id", sort=False).groups.items():
        indices = list(indices_obj)
        positions = {idx: pos for pos, idx in enumerate(indices)}
        for idx in indices:
            row = events.loc[idx]
            if (row["event_type"] != "Pass" or row["outcome"] != "Successful"
                    or pd.isna(row["player_id"]) or pd.isna(row["possession_id"])):
                continue
            pos = positions[idx]
            for candidate_idx in indices[pos + 1:pos + 1 + config.receiver_max_events]:
                candidate = events.loc[candidate_idx]
                if pd.isna(candidate["possession_id"]):
                    continue
                if candidate["possession_id"] != row["possession_id"]:
                    break
                elapsed = candidate["match_second"] - row["match_second"]
                if pd.notna(elapsed) and elapsed > config.receiver_max_seconds:
                    break
                if candidate["is_administrative"] or candidate["is_paired_duplicate"]:
                    continue
                if candidate["team_id"] != row["team_id"] or candidate["event_type"] not in ON_BALL_TYPES or pd.isna(candidate["player_id"]):
                    continue
                if pd.notna(row["end_x_m"]) and pd.notna(candidate["start_x_m"]):
                    distance = math.hypot(candidate["start_x_m"] - row["end_x_m"], candidate["start_y_m"] - row["end_y_m"])
                else:
                    distance = math.nan
                if pd.notna(distance) and distance > config.receiver_max_distance_m:
                    continue
                confidence = "high" if pd.notna(distance) and distance <= 3 and (pd.isna(elapsed) or elapsed <= 2) else "medium" if pd.notna(distance) and distance <= 8 and (pd.isna(elapsed) or elapsed <= 5) else "low"
                events.at[idx, "inferred_receiver_id"] = candidate["player_id"]
                events.at[idx, "inferred_receiver_name"] = candidate.get("player_name", pd.NA)
                events.at[idx, "receiver_event_uid"] = candidate["event_uid"]
                events.at[idx, "receiver_confidence"] = confidence
                events.at[idx, "receiver_distance_m"] = distance
                if pd.notna(distance) and config.carry_min_distance_m <= distance <= config.carry_max_distance_m and (pd.isna(elapsed) or elapsed <= config.carry_max_seconds):
                    events.at[candidate_idx, "inferred_carry_distance_m"] = distance
                    events.at[candidate_idx, "carry_origin_event_uid"] = row["event_uid"]
                break
    return events


def build_possessions(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    meaningful = events[events["possession_id"].notna() & ~events["is_paired_duplicate"]]
    for (match_id, possession_id), group in meaningful.groupby(["match_id", "possession_id"], sort=False):
        group = group.sort_values("event_index", kind="stable")
        last_types = group["event_type"].tolist()
        result = "goal" if "Goal" in last_types else "shot" if SHOT_TYPES.intersection(last_types) else "offside" if "OffsidePass" in last_types or "OffsideGiven" in last_types else "turnover" if group.iloc[-1]["effective_outcome"] == "failure" else "retained_or_period_end"
        rows.append({
            "match_id": match_id, "possession_id": int(possession_id), "team_id": group.iloc[0]["team_id"],
            "period": group.iloc[0]["period"], "start_second": group["match_second"].min(),
            "end_second": group["match_second"].max(), "duration_seconds": group["match_second"].max() - group["match_second"].min(),
            "event_count": len(group), "start_event_uid": group.iloc[0]["event_uid"],
            "end_event_uid": group.iloc[-1]["event_uid"], "start_zone": group.iloc[0]["start_zone"],
            "end_zone": group.iloc[-1]["end_zone"] or group.iloc[-1]["start_zone"],
            "pass_count": int(group["event_type"].isin(["Pass", "OffsidePass"]).sum()),
            "progressive_action_count": int(group["is_progressive"].sum()), "result": result,
        })
    return pd.DataFrame(rows)


def _safe_rate(numerator: float, denominator: float) -> float | None:
    return float(numerator / denominator) if denominator else None


def build_player_match_features(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    actors = events[events["player_id"].notna() & ~events["is_administrative"] & ~events["is_paired_duplicate"]]
    receptions = events[events["inferred_receiver_id"].notna()].groupby(["match_id", "inferred_receiver_id"], dropna=False).size()
    for (match_id, team_id, player_id), group in actors.groupby(["match_id", "team_id", "player_id"], sort=False):
        passes = group[group["event_type"].isin(["Pass", "OffsidePass"])]
        successful_passes = passes["effective_success"].sum()
        zones = group["start_zone"].dropna()
        zone_counts = zones.value_counts(normalize=True)
        row = {
            "match_id": match_id, "team_id": team_id, "player_id": player_id,
            "player_name": group["player_name"].dropna().iloc[0] if "player_name" in group and group["player_name"].notna().any() else None,
            "events": len(group), "touches": int(group.get("is_touch", pd.Series(False, index=group.index)).fillna(False).astype(bool).sum()),
            "passes_attempted": len(passes), "passes_completed": int(successful_passes),
            "pass_completion_rate": _safe_rate(successful_passes, len(passes)),
            "progressive_passes_attempted": int(passes["is_progressive"].sum()),
            "final_third_entries": int(passes["is_final_third_entry"].sum()), "box_entries": int(passes["is_box_entry"].sum()),
            "receptions_inferred": int(receptions.get((match_id, player_id), 0)),
            "carries_inferred": int(group["inferred_carry_distance_m"].notna().sum()),
            "carry_distance_m": group["inferred_carry_distance_m"].sum(min_count=1),
            "take_ons_attempted": int(group["event_type"].eq("TakeOn").sum()),
            "take_ons_completed": int((group["event_type"].eq("TakeOn") & group["effective_success"]).sum()),
            "shots": int(group["event_type"].isin(SHOT_TYPES).sum()), "goals": int(group["event_type"].eq("Goal").sum()),
            "recoveries": int(group["event_type"].isin(GAIN_TYPES).sum()), "tackles": int(group["event_type"].eq("Tackle").sum()),
            "interceptions": int(group["event_type"].eq("Interception").sum()),
            "average_action_x": group["x"].mean(), "average_action_y": group["y"].mean(),
            "dominant_zone": zones.mode().iloc[0] if not zones.empty else None,
        }
        for third in ("defensive", "middle", "attacking"):
            row[f"{third}_third_action_share"] = float(group["start_third"].eq(third).mean())
        for channel in ("left_wing", "left_half_space", "center", "right_half_space", "right_wing"):
            row[f"{channel}_action_share"] = float(group["start_channel"].eq(channel).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def build_profiles(features: pd.DataFrame, events: pd.DataFrame) -> list[dict]:
    profiles = []
    pass_events = events[events["event_type"].isin(["Pass", "OffsidePass"])]
    for row in features.to_dict("records"):
        player_passes = pass_events[(pass_events["match_id"] == row["match_id"]) & (pass_events["player_id"] == row["player_id"])]
        receivers = player_passes["inferred_receiver_name"].dropna().value_counts().head(5).to_dict()
        profiles.append({
            "match_id": row["match_id"], "team_id": row["team_id"], "player_id": row["player_id"], "player_name": row["player_name"],
            "role": {"dominant_zone": row["dominant_zone"], "average_action": {"x": row["average_action_x"], "y": row["average_action_y"]}},
            "passing": {"attempted": row["passes_attempted"], "completed": row["passes_completed"], "completion_rate": row["pass_completion_rate"], "progressive_attempted": row["progressive_passes_attempted"], "most_common_receivers": receivers},
            "receiving": {"receptions_inferred": row["receptions_inferred"]},
            "progression": {"carries_inferred": row["carries_inferred"], "carry_distance_m": row["carry_distance_m"]},
            "attacking": {"shots": row["shots"], "goals": row["goals"]},
            "defending": {"recoveries": row["recoveries"], "tackles": row["tackles"], "interceptions": row["interceptions"]},
        })
    return profiles


def digest_events(raw: pd.DataFrame, config: PipelineConfig | None = None) -> PipelineResult:
    """Run the full in-memory transformation; useful for tests and notebooks."""
    config = config or PipelineConfig()
    events = infer_receivers_and_carries(assign_possessions(pair_opponent_events(enrich_events(raw, config))), config)
    possessions = build_possessions(events)
    features = build_player_match_features(events)
    return PipelineResult(events, possessions, features, build_profiles(features, events))


def _fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {"path": str(path.resolve()), "bytes": stat.st_size, "modified_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def _atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA:
        return None
    return value


def _write_frame(frame: pd.DataFrame, path: Path, file_format: str) -> None:
    if file_format == "parquet":
        _atomic_write(path, lambda temporary: frame.to_parquet(temporary, index=False))
    else:
        _atomic_write(path, lambda temporary: frame.to_csv(temporary, index=False))


def run_pipeline(input_path: str | Path, output_directory: str | Path, *, file_format: str = "auto",
                 limit_matches: int | None = None, config: PipelineConfig | None = None) -> dict:
    """Read one stable export and atomically publish a complete derived run."""
    source = Path(input_path)
    output = Path(output_directory)
    if source.resolve() == output.resolve() or output.resolve() in source.resolve().parents:
        raise ValueError("Output must be separate from the input file")
    if not source.is_file():
        raise FileNotFoundError(source)
    if file_format == "auto":
        file_format = "parquet" if importlib.util.find_spec("pyarrow") or importlib.util.find_spec("fastparquet") else "csv"
    if file_format not in {"csv", "parquet"}:
        raise ValueError("file_format must be auto, csv, or parquet")
    if file_format == "parquet" and not (importlib.util.find_spec("pyarrow") or importlib.util.find_spec("fastparquet")):
        raise RuntimeError("Parquet output requires pyarrow or fastparquet; use --format csv or install the parquet extra")

    source_before = _fingerprint(source)
    raw = pd.read_csv(source, low_memory=False)
    source_after = _fingerprint(source)
    if source_before != source_after:
        raise RuntimeError("Input changed while it was being read; rerun against the collector's next stable export")
    if limit_matches is not None:
        if limit_matches < 1:
            raise ValueError("limit_matches must be positive")
        selected = raw["match_id"].drop_duplicates().head(limit_matches)
        raw = raw[raw["match_id"].isin(selected)].copy()

    result = digest_events(raw, config)
    extension = "parquet" if file_format == "parquet" else "csv"
    manifest = {
        "schema_version": 1, "pipeline_version": "0.1.0", "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source_before, "format": file_format, "config": asdict(config or PipelineConfig()),
        "row_counts": {"events_enriched": len(result.events_enriched), "possessions": len(result.possessions),
                       "player_match_features": len(result.player_match_features), "player_match_profiles": len(result.player_match_profiles)},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging.", dir=output.parent))
    artifacts = [f"events_enriched.{extension}", f"possessions.{extension}",
                 f"player_match_features.{extension}", "player_match_profiles.jsonl", "manifest.json"]
    try:
        _write_frame(result.events_enriched, staging / artifacts[0], file_format)
        _write_frame(result.possessions, staging / artifacts[1], file_format)
        _write_frame(result.player_match_features, staging / artifacts[2], file_format)
        _atomic_write(staging / artifacts[3], lambda p: p.write_text(
            "".join(json.dumps(_json_safe(profile), ensure_ascii=False, allow_nan=False) + "\n"
                    for profile in result.player_match_profiles), encoding="utf-8"
        ))
        _atomic_write(staging / artifacts[4], lambda p: p.write_text(json.dumps(manifest, indent=2), encoding="utf-8"))
        output.mkdir(parents=True, exist_ok=True)
        # The manifest is the commit marker and is deliberately installed last.
        for name in artifacts:
            os.replace(staging / name, output / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest
