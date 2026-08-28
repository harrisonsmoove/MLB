"""Retrosheet event file ingestion.

Source for the empirical base-out transition and baserunner advancement
matrices. Deliberately drawn from a deeper history than the Statcast window:
how often a runner on first scores on a double moves far more slowly than
hitter talent does, so extra seasons buy precision at little cost in bias.

``as_of_ts`` is the *publication* date, not the retrieval date. Retrosheet
posts a season's events the following spring, so stamping these rows with
today's timestamp would imply the 2024 event files were available during 2024
and let a 2024-season transition matrix be fit on 2024 data. The offset is in
config, and it means matrices for season N are necessarily fit on seasons < N.

Known gap: ``game_pk`` is left null. Retrosheet keys games as ``ANA202104010``
(park, date, game number) with its own team codes, and joining that to MLBAM
game_pk needs a team-code crosswalk that this milestone does not build. Nothing
downstream needs it -- transition matrices are estimated from base-out states,
not from individual games -- but it is a real hole and it is listed as one.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, time
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.ingest.retro_play import HalfInningState, parse_play
from mlb_edge.storage.rawcache import RawEntry

EVENT_FILE_SUFFIXES = (".EVA", ".EVN", ".EVE")


class RetrosheetIngester(Ingester):
    source_name = "retrosheet"
    writes_tables = ("retrosheet_events",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        configured = set(self.config.get("seasons", []) or [])
        requested = set(kwargs.get("seasons") or range(start.year, end.year + 1))
        seasons = sorted(configured & requested) if configured else sorted(requested)
        return [
            FetchTask(
                dataset="events",
                partition=str(season),
                url=self.config.endpoint("event_zip", season=season),
                content_type_hint="application/zip",
                # A published season's event file is final.
                max_age_seconds=None,
                context={"season": season},
            )
            for season in seasons
        ]

    def publication_ts(self, season: int) -> datetime:
        mmdd = str(self.config.get("publication_mmdd", "04-01"))
        offset = int(self.config.get("publication_year_offset", 1))
        month, day = (int(part) for part in mmdd.split("-"))
        return datetime.combine(
            date(season + offset, month, day), time.min, tzinfo=UTC
        )

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        season = int(entry.partition)
        as_of = self.publication_ts(season)
        prov = provenance_columns(entry, self.source_name)

        rows: list[dict[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                if not name.upper().endswith(EVENT_FILE_SUFFIXES):
                    continue
                text = archive.read(name).decode("latin-1")
                rows.extend(parse_event_file(text, season=season, as_of=as_of, prov=prov))

        return {"retrosheet_events": pl.DataFrame(rows) if rows else pl.DataFrame()}


def parse_event_file(
    text: str, *, season: int, as_of: datetime, prov: dict[str, Any]
) -> list[dict[str, Any]]:
    """Walk one ``.EVx`` file, emitting a row per play."""
    rows: list[dict[str, Any]] = []

    game_id: str | None = None
    game_date: date | None = None
    site: str | None = None
    event_num = 0
    state = HalfInningState()
    current_half: tuple[int, int] | None = None
    hands: dict[str, str] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        record = parts[0].lower()

        if record == "id":
            game_id = parts[1] if len(parts) > 1 else None
            game_date = None
            site = None
            event_num = 0
            state.reset()
            current_half = None
            hands.clear()
            continue

        if record == "info" and len(parts) >= 3:
            if parts[1] == "date":
                game_date = _parse_retro_date(parts[2])
            elif parts[1] == "site":
                site = parts[2]
            continue

        if record in ("start", "sub") and len(parts) >= 6:
            # Track handedness so matrices can be split by platoon later.
            hands[parts[1]] = ""
            continue

        if record != "play" or len(parts) < 7 or game_id is None:
            continue

        try:
            inning = int(parts[1])
            bat_home = int(parts[2])
        except ValueError:
            continue
        batter = parts[3]
        event_text = ",".join(parts[6:])  # the event field can contain commas

        half = (inning, bat_home)
        if current_half is not None and half != current_half:
            state.reset()
        current_half = half

        result = parse_play(event_text, state)
        event_num += 1
        rows.append(
            {
                "retro_game_id": game_id,
                "event_num": event_num,
                "game_pk": None,
                "season": season,
                "game_date": game_date,
                "park_id": site,
                "inning": inning,
                "bat_home_id": bat_home,
                "batter_retro_id": batter,
                "pitcher_retro_id": None,
                "bat_hand": None,
                "pit_hand": None,
                "outs_before": result.outs_before,
                "start_base_state": result.start_base_state,
                "end_base_state": result.end_base_state,
                "event_code": result.event_code,
                "event_text": result.raw,
                "runs_on_play": result.runs,
                "outs_on_play": result.outs,
                "rbi": None,
                "run1_dest": result.run1_dest,
                "run2_dest": result.run2_dest,
                "run3_dest": result.run3_dest,
                "batter_dest": result.batter_dest,
                "sb_flags": "ok" if result.parse_ok else "parse_failed",
                "as_of_ts": as_of,
                **prov,
            }
        )

    return rows


def _parse_retro_date(value: str) -> date | None:
    try:
        year, month, day = (int(part) for part in value.split("/"))
        return date(year, month, day)
    except (ValueError, AttributeError):
        return None
