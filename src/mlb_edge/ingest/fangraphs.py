"""FanGraphs projection ingestion.

Projections, not season-to-date rates, are the true-talent base. A .340 wOBA on
200 plate appearances is mostly sampling noise; a projection has already done
the regression properly, with an aging curve and a playing-time estimate
attached. Using the raw rate instead is one of the explicit do-nots.

Daily snapshots are the point. Backtesting a decision made on 12 May requires
knowing what the projection said on 12 May, not what it says today after three
more months of information. Every pull is stored under its snapshot date and
read point-in-time.

Systems worth knowing apart:
  * ``fangraphsdc`` (Depth Charts) -- a 50/50 Steamer/ZiPS blend prorated to
    RosterResource playing-time estimates. The playing time is the value add.
  * ``thebatx`` -- hitters only, incorporates Statcast batted-ball data.
  * Most systems update daily in-season.

The response field names below are driven by ``field_map`` in settings.yaml
because they could not be verified against the live API from this environment.
A wrong map produces null columns, not an exception, and ``mlb-edge verify``
reports the null rate so the failure is visible rather than silent.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.storage.rawcache import RawEntry


class FangraphsProjectionsIngester(Ingester):
    source_name = "fangraphs"
    writes_tables = ("projections",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        """One task per (system, player_type) for the snapshot date.

        Projections are a *today* resource: there is no endpoint that returns
        what a system said last April. Backfilling history is therefore
        impossible, and pretending otherwise by stamping today's projection with
        a past date would be the purest form of lookahead. The snapshot date is
        always today's date, and a backtest simply has no projection rows before
        the day this ingester first ran.
        """
        snapshot = kwargs.get("snapshot_date") or end
        systems = self.config.get("systems", {}) or {}
        request = self.config.get("request", {}) or {}

        tasks: list[FetchTask] = []
        for stats_key, group in (("bat", "batting"), ("pit", "pitching")):
            for system in systems.get(group, []) or []:
                params = {"type": system, "stats": stats_key, **request}
                tasks.append(
                    FetchTask(
                        dataset="projections",
                        partition=f"{snapshot.isoformat()}_{system}_{stats_key}",
                        url=self.config.endpoint("projections"),
                        params=params,
                        max_age_seconds=None,  # a dated snapshot is immutable
                        context={
                            "system": system,
                            "player_type": "batter" if stats_key == "bat" else "pitcher",
                            "snapshot_date": snapshot.isoformat(),
                        },
                    )
                )
        return tasks

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        data = json.loads(payload)
        records = _records(data)
        if not records:
            return {"projections": pl.DataFrame()}

        system, player_type, snapshot = _context_from(entry, task)
        field_map = self.config.get("field_map", {}) or {}
        prov = provenance_columns(entry, self.source_name)

        rows: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            mlbam = _first(record, field_map.get("player_id_mlbam", []))
            fangraphs_id = _first(record, field_map.get("player_id_fangraphs", []))
            player_id = _to_int(mlbam)
            if player_id is None:
                # No MLBAM id means the row cannot be joined to game data. Keep
                # it anyway with a null key so the unresolved rate is
                # measurable, rather than dropping it and reporting a clean
                # ingest over a silently thinned projection set.
                pass

            singles = _singles(record, field_map)
            rows.append(
                {
                    "system": system,
                    "player_id": player_id,
                    "player_type": player_type,
                    "snapshot_date": snapshot,
                    "player_name": _first(record, field_map.get("player_name", [])),
                    "team": _first(record, field_map.get("team", [])),
                    "pa": _to_float(_first(record, field_map.get("pa", []))),
                    "ab": _to_float(_first(record, field_map.get("ab", []))),
                    "ip": _to_float(_first(record, field_map.get("ip", []))),
                    "tbf": _to_float(_first(record, field_map.get("tbf", []))),
                    "k": _to_float(_first(record, field_map.get("k", []))),
                    "bb": _to_float(_first(record, field_map.get("bb", []))),
                    "hbp": _to_float(_first(record, field_map.get("hbp", []))),
                    "singles": singles,
                    "doubles": _to_float(_first(record, field_map.get("doubles", []))),
                    "triples": _to_float(_first(record, field_map.get("triples", []))),
                    "hr": _to_float(_first(record, field_map.get("hr", []))),
                    "woba": _to_float(_first(record, field_map.get("woba", []))),
                    "wrc_plus": _to_float(_first(record, field_map.get("wrc_plus", []))),
                    "era": _to_float(_first(record, field_map.get("era", []))),
                    "fip": _to_float(_first(record, field_map.get("fip", []))),
                    "k_pct": _to_float(_first(record, field_map.get("k_pct", []))),
                    "bb_pct": _to_float(_first(record, field_map.get("bb_pct", []))),
                    "gb_pct": _to_float(_first(record, field_map.get("gb_pct", []))),
                    "fb_pct": _to_float(_first(record, field_map.get("fb_pct", []))),
                    # The whole record is retained so that a field_map
                    # correction can be applied by reparsing the warehouse
                    # rather than re-fetching a snapshot that no longer exists.
                    "extras": json.dumps(
                        {"fangraphs_id": fangraphs_id, "raw": record}, default=str
                    ),
                    "as_of_ts": entry.retrieved_ts,
                    **prov,
                }
            )

        frame = pl.DataFrame(rows) if rows else pl.DataFrame()
        # Rows with no MLBAM id are unjoinable; keep them out of the keyed table
        # but the count is preserved in the ingest report via rows_offered.
        if not frame.is_empty():
            frame = frame.filter(pl.col("player_id").is_not_null())
        return {"projections": frame}


def _records(data: Any) -> list[Any]:
    """Find the record list, whether the payload is a list or a wrapper object."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "projections", "results", "players"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _context_from(entry: RawEntry, task: FetchTask | None) -> tuple[str, str, date]:
    """Recover (system, player_type, snapshot_date) from the task or partition.

    The partition name is the fallback so that ``reload_from_cache`` -- which
    has no task -- still reconstructs the right rows from bytes alone.
    """
    if task is not None and task.context:
        return (
            str(task.context["system"]),
            str(task.context["player_type"]),
            date.fromisoformat(str(task.context["snapshot_date"])),
        )
    snapshot_str, _, remainder = entry.partition.partition("_")
    system, _, stats_key = remainder.rpartition("_")
    return (
        system or "unknown",
        "batter" if stats_key == "bat" else "pitcher",
        date.fromisoformat(snapshot_str),
    )


def _first(record: dict[str, Any], candidates: list[str]) -> Any:
    for key in candidates:
        if key in record and record[key] not in ("", None):
            return record[key]
    return None


def _singles(record: dict[str, Any], field_map: dict[str, Any]) -> float | None:
    """Singles are rarely projected directly; derive from H - 2B - 3B - HR."""
    hits = _to_float(_first(record, field_map.get("hits", [])))
    if hits is None:
        return None
    parts = [
        _to_float(_first(record, field_map.get(name, []))) or 0.0
        for name in ("doubles", "triples", "hr")
    ]
    return hits - sum(parts)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
