"""MLB Stats API ingestion.

The authoritative source for game_pk, schedule, probables, lineups, officials
and results. Free and unauthenticated.

Split into several ingesters rather than one, because the dependencies form a
DAG: you cannot plan game-feed fetches until the schedule has told you which
game_pks exist. Keeping ``plan()`` free of network calls is what lets the whole
pipeline be dry-run and reasoned about.

Everything is keyed on ``game_pk``. Never on ``(date, home, away)``: that key
collides on doubleheaders, and a game suspended on Tuesday and resumed on
Wednesday carries a date that matches neither naive expectation.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl

from mlb_edge.hydrate import hydrate_string
from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import game_date_for, parse_iso_utc, utcnow

# Status codes that mean the game is over and its payload will not change again.
FINAL_STATUS_CODES = {"F", "FR", "FT", "O", "DI", "CR", "DC"}


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a nested mapping defensively.

    Upstream payload shapes drift. A missing key must produce a null column, not
    a KeyError that aborts a season-long backfill twelve games in.
    """
    current = obj
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current if current is not None else default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MlbScheduleIngester(Ingester):
    """Schedule, probable pitchers and the games spine."""

    source_name = "mlb_statsapi"
    writes_tables = ("games", "probable_pitchers", "venues")

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        today = kwargs.get("today") or utcnow().date()
        tasks: list[FetchTask] = []
        cursor = start
        while cursor <= end:
            # A week at a time: the payload stays small and a partition boundary
            # that lands mid-slate is harmless because everything is keyed on
            # game_pk, not on which request happened to carry it.
            chunk_end = min(cursor + timedelta(days=6), end)
            terms = self.config.get("schedule_hydrate") or []
            tasks.append(
                FetchTask(
                    dataset="schedule",
                    partition=f"{cursor.isoformat()}_{chunk_end.isoformat()}",
                    url=self.config.endpoint(
                        "schedule",
                        start=cursor.isoformat(),
                        end=chunk_end.isoformat(),
                        hydrate=hydrate_string(terms),
                    ),
                    # StatsAPI 406s the whole request on one unrecognised
                    # hydrate term. The games spine matters more than the
                    # hydrated extras, so a rejection degrades to the bare call
                    # rather than losing the range entirely.
                    fallback_url=self.config.endpoint(
                        "schedule_minimal",
                        start=cursor.isoformat(),
                        end=chunk_end.isoformat(),
                    ),
                    fallback_note=(
                        "probable_pitchers, venue, weather and linescore are ABSENT "
                        "for this range -- they are not 'not announced'. Run "
                        "`mlb-edge probe-hydrate` to find the rejected term, fix "
                        "sources.mlb_statsapi.schedule_hydrate, then re-run with "
                        "--force-refresh."
                    ),
                    max_age_seconds=_schedule_ttl(chunk_end, today),
                    context={"start": cursor.isoformat(), "end": chunk_end.isoformat()},
                )
            )
            cursor = chunk_end + timedelta(days=1)
        return tasks

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        import json

        data = json.loads(payload)
        as_of = entry.retrieved_ts
        prov = provenance_columns(entry, self.source_name)

        games: list[dict[str, Any]] = []
        probables: list[dict[str, Any]] = []
        venues: list[dict[str, Any]] = []
        seen_venues: set[int] = set()

        for day in data.get("dates", []) or []:
            for game in day.get("games", []) or []:
                game_pk = _as_int(game.get("gamePk"))
                if game_pk is None:
                    continue
                start_raw = game.get("gameDate")
                if not start_raw:
                    continue
                start_ts = parse_iso_utc(start_raw)

                venue_id = _as_int(_get(game, "venue", "id"))
                # officialDate is MLB's own slate date and is the right answer
                # for suspended/resumed games, where park-local midnight is not.
                official = game.get("officialDate")
                local_date = (
                    date.fromisoformat(official)
                    if official
                    else game_date_for(start_ts, self.settings.timezone)
                )

                games.append(
                    {
                        "game_pk": game_pk,
                        "season": _as_int(game.get("season")) or start_ts.year,
                        "game_type": game.get("gameType"),
                        "game_date_local": local_date,
                        "scheduled_start_ts": start_ts,
                        "status": _get(game, "status", "detailedState"),
                        "status_code": _get(game, "status", "statusCode"),
                        "home_team_id": _as_int(_get(game, "teams", "home", "team", "id")),
                        "away_team_id": _as_int(_get(game, "teams", "away", "team", "id")),
                        "venue_id": venue_id,
                        "doubleheader": game.get("doubleHeader"),
                        "game_number": _as_int(game.get("gameNumber")),
                        "series_game_number": _as_int(game.get("seriesGameNumber")),
                        "games_in_series": _as_int(game.get("gamesInSeries")),
                        "day_night": game.get("dayNight"),
                        "scheduled_innings": _as_int(game.get("scheduledInnings")),
                        "resume_of_game_pk": _as_int(
                            game.get("resumedFromGamePk") or game.get("resumeGamePk")
                        ),
                        "as_of_ts": as_of,
                        **prov,
                    }
                )

                for side in ("home", "away"):
                    pitcher = _get(game, "teams", side, "probablePitcher", default={})
                    pitcher_id = _as_int(pitcher.get("id")) if isinstance(pitcher, dict) else None
                    if pitcher_id is None:
                        continue
                    probables.append(
                        {
                            "game_pk": game_pk,
                            "side": side,
                            "pitcher_id": pitcher_id,
                            "pitcher_name": pitcher.get("fullName"),
                            "throws": _get(pitcher, "pitchHand", "code"),
                            # The schedule feed never marks a probable as
                            # confirmed; only the game feed's starter does.
                            "is_confirmed": False,
                            "as_of_ts": as_of,
                            **prov,
                        }
                    )

                if venue_id is not None and venue_id not in seen_venues:
                    seen_venues.add(venue_id)
                    venues.append(
                        {
                            "venue_id": venue_id,
                            "name": _get(game, "venue", "name"),
                            "as_of_ts": as_of,
                            **prov,
                        }
                    )

        return {
            "games": pl.DataFrame(games) if games else pl.DataFrame(),
            "probable_pitchers": pl.DataFrame(probables) if probables else pl.DataFrame(),
            "venues": pl.DataFrame(venues) if venues else pl.DataFrame(),
        }


class MlbGameFeedIngester(Ingester):
    """Lineups, officials, results and per-player game lines.

    Plans from the ``games`` table, so the schedule ingester must have run
    first. The dependency is explicit rather than implicit for a reason: a feed
    fetch for a game_pk we have never seen would create an orphan row that no
    later join could repair.
    """

    source_name = "mlb_statsapi"
    writes_tables = (
        "lineup_slots",
        "umpire_assignments",
        "game_results",
        "pitcher_game_stats",
        "batter_game_stats",
        "pitcher_appearances",
        "probable_pitchers",
    )

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        if self.warehouse is None:
            raise RuntimeError("MlbGameFeedIngester.plan needs a warehouse to read game_pks from")

        game_types = kwargs.get("game_types") or ("R",)
        placeholders = ", ".join("?" for _ in game_types)
        rows = self.warehouse.sql(
            f"""
            SELECT game_pk, any_value(status_code) AS status_code
            FROM (
                SELECT game_pk, status_code,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) AS rn
                FROM games
                WHERE game_date_local BETWEEN ? AND ?
                  AND game_type IN ({placeholders})
            )
            WHERE rn = 1
            GROUP BY game_pk
            ORDER BY game_pk
            """,
            [start, end, *game_types],
        )

        tasks: list[FetchTask] = []
        for row in rows.iter_rows(named=True):
            game_pk = int(row["game_pk"])
            is_final = (row["status_code"] or "") in FINAL_STATUS_CODES
            tasks.append(
                FetchTask(
                    dataset="game_feed",
                    partition=str(game_pk),
                    url=self.config.endpoint("game_feed", game_pk=game_pk),
                    # A final game's feed is immutable, so never re-pull it.
                    # Anything else gets a short TTL because lineups move.
                    max_age_seconds=None if is_final else 300.0,
                    context={"game_pk": game_pk},
                )
            )
        return tasks

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        import json

        data = json.loads(payload)
        as_of = entry.retrieved_ts
        prov = provenance_columns(entry, self.source_name)

        game_pk = _as_int(_get(data, "gamePk")) or _as_int(_get(data, "gameData", "game", "pk"))
        if game_pk is None:
            return {}

        status_code = _get(data, "gameData", "status", "statusCode")
        is_final = (status_code or "") in FINAL_STATUS_CODES

        players_meta = _get(data, "gameData", "players", default={}) or {}
        boxscore_teams = _get(data, "liveData", "boxscore", "teams", default={}) or {}

        lineups = self._parse_lineups(boxscore_teams, players_meta, game_pk, as_of, prov)
        officials = self._parse_officials(data, game_pk, as_of, prov)
        starters = self._parse_confirmed_starters(boxscore_teams, game_pk, as_of, prov)
        pitcher_stats, batter_stats, appearances = self._parse_box_lines(
            data, boxscore_teams, game_pk, as_of, prov
        )
        results = self._parse_result(data, game_pk, status_code, is_final, as_of, prov)

        frames = {
            "lineup_slots": pl.DataFrame(lineups) if lineups else pl.DataFrame(),
            "umpire_assignments": pl.DataFrame(officials) if officials else pl.DataFrame(),
            "probable_pitchers": pl.DataFrame(starters) if starters else pl.DataFrame(),
        }
        # Outcome rows are only emitted for final games. A suspended or
        # in-progress game has no result, and writing a partial one would put a
        # half-played score into the label table.
        if is_final:
            frames["game_results"] = pl.DataFrame([results]) if results else pl.DataFrame()
            frames["pitcher_game_stats"] = pl.DataFrame(pitcher_stats) if pitcher_stats else pl.DataFrame()
            frames["batter_game_stats"] = pl.DataFrame(batter_stats) if batter_stats else pl.DataFrame()
            frames["pitcher_appearances"] = pl.DataFrame(appearances) if appearances else pl.DataFrame()
        return frames

    # -- section parsers -----------------------------------------------------
    def _parse_lineups(
        self,
        boxscore_teams: dict[str, Any],
        players_meta: dict[str, Any],
        game_pk: int,
        as_of: Any,
        prov: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for side in ("home", "away"):
            team = boxscore_teams.get(side) or {}
            order = team.get("battingOrder") or []
            team_players = team.get("players") or {}
            # A populated battingOrder in the boxscore IS the official card;
            # MLB does not publish a projected one. Anything shorter than nine
            # is a partial write mid-posting, not a lineup.
            confirmed = len(order) >= 9
            for index, player_id in enumerate(order[:9], start=1):
                pid = _as_int(player_id)
                if pid is None:
                    continue
                key = f"ID{pid}"
                detail = team_players.get(key) or {}
                meta = players_meta.get(key) or {}
                rows.append(
                    {
                        "game_pk": game_pk,
                        "side": side,
                        "batting_order": index,
                        "player_id": pid,
                        "player_name": meta.get("fullName") or _get(detail, "person", "fullName"),
                        "position": _get(detail, "position", "abbreviation"),
                        "bats": _get(meta, "batSide", "code"),
                        "is_confirmed": confirmed,
                        "as_of_ts": as_of,
                        **prov,
                    }
                )
        return rows

    def _parse_officials(
        self, data: dict[str, Any], game_pk: int, as_of: Any, prov: dict[str, Any]
    ) -> list[dict[str, Any]]:
        officials = _get(data, "liveData", "boxscore", "officials", default=[]) or []
        rows: list[dict[str, Any]] = []
        for official in officials:
            role = official.get("officialType")
            person = official.get("official") or {}
            if not role:
                continue
            rows.append(
                {
                    "game_pk": game_pk,
                    "role": role,
                    "umpire_id": _as_int(person.get("id")),
                    "umpire_name": person.get("fullName"),
                    "as_of_ts": as_of,
                    **prov,
                }
            )
        return rows

    def _parse_confirmed_starters(
        self, boxscore_teams: dict[str, Any], game_pk: int, as_of: Any, prov: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The actual starter, once the feed names one, as a confirmed probable."""
        rows: list[dict[str, Any]] = []
        for side in ("home", "away"):
            team = boxscore_teams.get(side) or {}
            pitchers = team.get("pitchers") or []
            if not pitchers:
                continue
            starter_id = _as_int(pitchers[0])
            if starter_id is None:
                continue
            detail = (team.get("players") or {}).get(f"ID{starter_id}") or {}
            rows.append(
                {
                    "game_pk": game_pk,
                    "side": side,
                    "pitcher_id": starter_id,
                    "pitcher_name": _get(detail, "person", "fullName"),
                    "throws": None,
                    "is_confirmed": True,
                    "as_of_ts": as_of,
                    **prov,
                }
            )
        return rows

    def _parse_box_lines(
        self,
        data: dict[str, Any],
        boxscore_teams: dict[str, Any],
        game_pk: int,
        as_of: Any,
        prov: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        pitcher_rows: list[dict[str, Any]] = []
        batter_rows: list[dict[str, Any]] = []
        appearance_rows: list[dict[str, Any]] = []

        official_date = _get(data, "gameData", "datetime", "officialDate")
        local_date = date.fromisoformat(official_date) if official_date else None

        for side in ("home", "away"):
            team = boxscore_teams.get(side) or {}
            team_id = _as_int(_get(team, "team", "id"))
            pitcher_ids = [pid for pid in (team.get("pitchers") or [])]
            order = team.get("battingOrder") or []
            for key, detail in (team.get("players") or {}).items():
                pid = _as_int(str(key).removeprefix("ID"))
                if pid is None:
                    continue
                pitching = _get(detail, "stats", "pitching", default={}) or {}
                batting = _get(detail, "stats", "batting", default={}) or {}

                if pitching:
                    outs = _innings_to_outs(pitching.get("inningsPitched"))
                    is_start = bool(pitcher_ids and _as_int(pitcher_ids[0]) == pid)
                    pitcher_rows.append(
                        {
                            "game_pk": game_pk,
                            "player_id": pid,
                            "team_id": team_id,
                            "is_start": is_start,
                            "outs_recorded": outs,
                            "batters_faced": _as_int(pitching.get("battersFaced")),
                            "pitches_thrown": _as_int(pitching.get("numberOfPitches"))
                            or _as_int(pitching.get("pitchesThrown")),
                            "strikeouts": _as_int(pitching.get("strikeOuts")),
                            "walks": _as_int(pitching.get("baseOnBalls")),
                            "hits_allowed": _as_int(pitching.get("hits")),
                            "home_runs_allowed": _as_int(pitching.get("homeRuns")),
                            "earned_runs": _as_int(pitching.get("earnedRuns")),
                            "as_of_ts": as_of,
                            **prov,
                        }
                    )
                    appearance_rows.append(
                        {
                            "game_pk": game_pk,
                            "player_id": pid,
                            "team_id": team_id,
                            "game_date_local": local_date,
                            "is_start": is_start,
                            "pitches_thrown": _as_int(pitching.get("numberOfPitches"))
                            or _as_int(pitching.get("pitchesThrown")),
                            "batters_faced": _as_int(pitching.get("battersFaced")),
                            "outs_recorded": outs,
                            "entered_inning": None,
                            "leverage_index": None,
                            "as_of_ts": as_of,
                            **prov,
                        }
                    )

                if batting and batting.get("plateAppearances") is not None:
                    slot = None
                    if pid in [_as_int(p) for p in order]:
                        slot = [_as_int(p) for p in order].index(pid) + 1
                    batter_rows.append(
                        {
                            "game_pk": game_pk,
                            "player_id": pid,
                            "team_id": team_id,
                            "batting_order": slot,
                            "plate_appearances": _as_int(batting.get("plateAppearances")),
                            "at_bats": _as_int(batting.get("atBats")),
                            "hits": _as_int(batting.get("hits")),
                            "doubles": _as_int(batting.get("doubles")),
                            "triples": _as_int(batting.get("triples")),
                            "home_runs": _as_int(batting.get("homeRuns")),
                            "walks": _as_int(batting.get("baseOnBalls")),
                            "strikeouts": _as_int(batting.get("strikeOuts")),
                            "runs": _as_int(batting.get("runs")),
                            "rbi": _as_int(batting.get("rbi")),
                            "as_of_ts": as_of,
                            **prov,
                        }
                    )

        return pitcher_rows, batter_rows, appearance_rows

    def _parse_result(
        self,
        data: dict[str, Any],
        game_pk: int,
        status_code: str | None,
        is_final: bool,
        as_of: Any,
        prov: dict[str, Any],
    ) -> dict[str, Any] | None:
        linescore = _get(data, "liveData", "linescore", default={}) or {}
        innings = linescore.get("innings") or []
        if not innings:
            return None

        home_total = _as_int(_get(linescore, "teams", "home", "runs"))
        away_total = _as_int(_get(linescore, "teams", "away", "runs"))

        # First five innings, for F5 markets. Summed from the linescore rather
        # than derived from the final, because a game that ends early has fewer
        # than five and must not silently report a five-inning score.
        home_f5 = away_f5 = 0
        innings_with_five = 0
        for inning in innings:
            num = _as_int(inning.get("num")) or 0
            if num <= 5:
                home_f5 += _as_int(_get(inning, "home", "runs")) or 0
                away_f5 += _as_int(_get(inning, "away", "runs")) or 0
                innings_with_five = max(innings_with_five, num)

        last = innings[-1]
        # The home half of the final inning is skipped when the home team is
        # already ahead. That missing half-inning is exactly why a naive
        # runs-per-inning model over-predicts home scoring.
        home_half_played = _get(last, "home", "runs") is not None

        return {
            "game_pk": game_pk,
            "home_runs": home_total,
            "away_runs": away_total,
            "home_runs_f5": home_f5 if innings_with_five >= 5 else None,
            "away_runs_f5": away_f5 if innings_with_five >= 5 else None,
            "innings_played": float(_as_int(linescore.get("currentInning")) or len(innings)),
            "home_won": (home_total > away_total) if None not in (home_total, away_total) else None,
            "went_extras": len(innings) > 9,
            "home_half_9_played": home_half_played,
            "status_code": status_code,
            "is_final": is_final,
            "as_of_ts": as_of,
            **prov,
        }


class MlbTransactionsIngester(Ingester):
    """Roster moves and IL transactions."""

    source_name = "mlb_statsapi"
    writes_tables = ("transactions",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        tasks: list[FetchTask] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=29), end)
            tasks.append(
                FetchTask(
                    dataset="transactions",
                    partition=f"{cursor.isoformat()}_{chunk_end.isoformat()}",
                    url=self.config.endpoint(
                        "transactions", start=cursor.isoformat(), end=chunk_end.isoformat()
                    ),
                    max_age_seconds=86400.0,
                )
            )
            cursor = chunk_end + timedelta(days=1)
        return tasks

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        import json

        data = json.loads(payload)
        as_of = entry.retrieved_ts
        prov = provenance_columns(entry, self.source_name)

        rows: list[dict[str, Any]] = []
        for item in data.get("transactions", []) or []:
            txn_id = _as_int(item.get("id"))
            if txn_id is None:
                continue
            rows.append(
                {
                    "transaction_id": txn_id,
                    "player_id": _as_int(_get(item, "person", "id")),
                    "team_id": _as_int(_get(item, "toTeam", "id")),
                    "type_code": item.get("typeCode"),
                    "description": item.get("description"),
                    "effective_date": _parse_date(item.get("effectiveDate") or item.get("date")),
                    "resolution_date": _parse_date(item.get("resolutionDate")),
                    "as_of_ts": as_of,
                    **prov,
                }
            )
        return {"transactions": pl.DataFrame(rows) if rows else pl.DataFrame()}


class MlbTeamsIngester(Ingester):
    """Team dimension, per season (teams change division and identity)."""

    source_name = "mlb_statsapi"
    writes_tables = ("teams",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        seasons = sorted({start.year, end.year} | set(kwargs.get("seasons") or []))
        return [
            FetchTask(
                dataset="teams",
                partition=str(season),
                url=self.config.endpoint("teams", season=season),
                max_age_seconds=86400.0 * 7,
                context={"season": season},
            )
            for season in seasons
        ]

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        import json

        data = json.loads(payload)
        as_of = entry.retrieved_ts
        prov = provenance_columns(entry, self.source_name)
        season = _as_int(entry.partition)

        rows: list[dict[str, Any]] = []
        for team in data.get("teams", []) or []:
            team_id = _as_int(team.get("id"))
            if team_id is None:
                continue
            rows.append(
                {
                    "team_id": team_id,
                    "season": _as_int(team.get("season")) or season,
                    "name": team.get("name"),
                    "abbreviation": team.get("abbreviation"),
                    "league": _get(team, "league", "name"),
                    "division": _get(team, "division", "name"),
                    "home_venue_id": _as_int(_get(team, "venue", "id")),
                    "as_of_ts": as_of,
                    **prov,
                }
            )
        return {"teams": pl.DataFrame(rows) if rows else pl.DataFrame()}


class MlbVenuesIngester(Ingester):
    """Venue dimension, merged with the local overrides in parks.yaml."""

    source_name = "mlb_statsapi"
    writes_tables = ("venues",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        return [
            FetchTask(
                dataset="venues",
                partition="all",
                url=self.config.endpoint("venues"),
                max_age_seconds=86400.0 * 7,
            )
        ]

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        import json

        data = json.loads(payload)
        as_of = entry.retrieved_ts
        prov = provenance_columns(entry, self.source_name)
        overrides = _park_overrides(self.settings)

        rows: list[dict[str, Any]] = []
        for venue in data.get("venues", []) or []:
            venue_id = _as_int(venue.get("id"))
            name = venue.get("name")
            if venue_id is None or not name:
                continue
            override = overrides.get(_normalise(name), {})
            rows.append(
                {
                    "venue_id": venue_id,
                    "name": name,
                    "slug": override.get("slug"),
                    "city": _get(venue, "location", "city"),
                    "state": _get(venue, "location", "stateAbbrev"),
                    "latitude": _to_float(_get(venue, "location", "defaultCoordinates", "latitude")),
                    "longitude": _to_float(
                        _get(venue, "location", "defaultCoordinates", "longitude")
                    ),
                    "timezone": _get(venue, "timeZone", "id"),
                    "altitude_ft": override.get("altitude_ft"),
                    "roof": override.get("roof", "none"),
                    # Deliberately null until derived. features/environment.py
                    # raises rather than resolving a wind vector without it.
                    "cf_bearing_deg": override.get("cf_bearing_deg"),
                    "orientation_source": override.get("orientation_source", "unset"),
                    "left_line_ft": _as_int(_get(venue, "fieldInfo", "leftLine")),
                    "center_ft": _as_int(_get(venue, "fieldInfo", "center")),
                    "right_line_ft": _as_int(_get(venue, "fieldInfo", "rightLine")),
                    "as_of_ts": as_of,
                    **prov,
                }
            )
        return {"venues": pl.DataFrame(rows) if rows else pl.DataFrame()}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _schedule_ttl(chunk_end: date, today: date) -> float | None:
    """How stale a cached schedule chunk may be.

    Recent and future slates move constantly (postponements, probables). Old
    ones move rarely but not never -- a suspended game gets a resumption date
    weeks later -- so they get a long TTL rather than being frozen forever.
    """
    age_days = (today - chunk_end).days
    if age_days < 2:
        return 900.0
    if age_days < 30:
        return 86400.0
    return 86400.0 * 30


def _innings_to_outs(value: Any) -> int | None:
    """Convert baseball's base-3 innings notation ("6.2" = 6 innings 2 outs)."""
    if value in (None, ""):
        return None
    try:
        whole_str, _, frac = str(value).partition(".")
        whole = int(whole_str or 0)
        partial = int(frac[0]) if frac else 0
    except ValueError:
        return None
    if partial not in (0, 1, 2):
        return None
    return whole * 3 + partial


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _park_overrides(settings: Any) -> dict[str, dict[str, Any]]:
    """Index parks.yaml by normalised venue name, including historical aliases."""
    defaults = settings.parks.get("defaults", {}) or {}
    index: dict[str, dict[str, Any]] = {}
    for park in settings.parks.get("parks", []) or []:
        names = (park.get("match") or {}).get("venue_name")
        if isinstance(names, str):
            names = [names]
        merged = {**defaults, **{k: v for k, v in park.items() if k not in ("match",)}}
        for name in names or []:
            index[_normalise(name)] = merged
    return index
