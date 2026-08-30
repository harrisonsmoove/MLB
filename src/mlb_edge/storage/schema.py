"""Warehouse schema registry.

This module is the single source of truth about what tables exist, how they are
keyed, and -- critically -- whether they may be read while building features.
The point-in-time test suite is written against this registry rather than
against a hand-maintained list, so a new table cannot be added without
declaring its leak posture.

Three invariants, enforced by tests in ``tests/test_schema_contract.py``:

1. Every table carries an ``as_of_ts`` column, except tables explicitly marked
   ``META`` with a written exemption reason.
2. Every table is keyed on ``game_pk`` where a game is involved. Never on
   ``(date, home_team, away_team)`` -- that key collides on doubleheaders and
   silently mis-joins suspended games resumed on a later date.
3. Tables holding realised results (``OUTCOME``) or closing prices
   (``CLOSING``) are not reachable from the ordinary feature reader. Closing
   lines are reachable *only* from the CLV evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import duckdb


class TableKind(StrEnum):
    META = "meta"
    """Audit and manifest tables. Not features, exempt from as_of_ts."""

    DIMENSION = "dimension"
    """Slowly-changing descriptive data. Feature-readable, point-in-time."""

    FACT = "fact"
    """Time-stamped observations. Feature-readable, point-in-time."""

    OUTCOME = "outcome"
    """Realised results -- the labels. Readable only via an explicit opt-in."""

    CLOSING = "closing"
    """Closing prices. Evaluation only. Never a feature, under any flag."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    kind: TableKind
    key: tuple[str, ...]
    ddl: str
    as_of_column: str | None = "as_of_ts"
    event_ts_column: str | None = None
    dynamic_columns: bool = False
    required_columns: tuple[str, ...] = ()
    exempt_reason: str | None = None
    notes: str = ""
    indexes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def feature_readable(self) -> bool:
        """Whether the ordinary feature reader may touch this table."""
        return self.kind in (TableKind.DIMENSION, TableKind.FACT)

    @property
    def quarantined(self) -> bool:
        return self.kind in (TableKind.OUTCOME, TableKind.CLOSING)


# ---------------------------------------------------------------------------
# Column fragments shared across tables.
# ---------------------------------------------------------------------------
_PROVENANCE = """
    source              TEXT NOT NULL,
    source_partition    TEXT,
    raw_sha256          TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL
"""

TABLES: tuple[TableSpec, ...] = (
    # -----------------------------------------------------------------------
    # Meta
    # -----------------------------------------------------------------------
    TableSpec(
        name="raw_manifest",
        kind=TableKind.META,
        key=("source", "dataset", "partition", "retrieved_at"),
        as_of_column=None,
        exempt_reason=(
            "Index of cached raw payloads. Its own retrieved_at IS the as-of "
            "axis; adding a second one would be a duplicate with a different name."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS raw_manifest (
            source            TEXT NOT NULL,
            dataset           TEXT NOT NULL,
            partition         TEXT NOT NULL,
            path              TEXT NOT NULL,
            retrieved_at      TIMESTAMPTZ NOT NULL,
            content_sha256    TEXT NOT NULL,
            n_bytes           BIGINT NOT NULL,
            content_type      TEXT,
            request_url       TEXT,
            request_params    JSON,
            upstream_status   INTEGER,
            PRIMARY KEY (source, dataset, partition, retrieved_at, path)
        )
        """,
    ),
    TableSpec(
        name="poll_import_log",
        kind=TableKind.META,
        key=("archive_path", "content_sha256"),
        as_of_column=None,
        exempt_reason=(
            "Bookkeeping for which poll-archive parquet files have been parsed "
            "into the warehouse. Carries no game data and is never joined to one."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS poll_import_log (
            archive_path      TEXT NOT NULL,
            content_sha256    TEXT NOT NULL,
            venue             TEXT,
            records_read      BIGINT,
            rows_written      BIGINT,
            unresolved        BIGINT,
            imported_at       TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (archive_path, content_sha256)
        )
        """,
    ),
    TableSpec(
        name="ingest_runs",
        kind=TableKind.META,
        key=("run_id",),
        as_of_column=None,
        exempt_reason="Operational audit log of ingest attempts. Never joined to a game.",
        ddl="""
        CREATE TABLE IF NOT EXISTS ingest_runs (
            run_id            TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            dataset           TEXT NOT NULL,
            range_start       DATE,
            range_end         DATE,
            started_at        TIMESTAMPTZ NOT NULL,
            finished_at       TIMESTAMPTZ,
            status            TEXT NOT NULL,
            rows_written      BIGINT DEFAULT 0,
            versions_written  BIGINT DEFAULT 0,
            error             TEXT
        )
        """,
    ),
    # -----------------------------------------------------------------------
    # Dimensions
    # -----------------------------------------------------------------------
    TableSpec(
        name="venues",
        kind=TableKind.DIMENSION,
        key=("venue_id",),
        notes=(
            "Keyed by venue, never by team. Teams relocate mid-window "
            "(Athletics to Sacramento 2025, Rays to Steinbrenner Field 2025, "
            "Blue Jays to Buffalo/Dunedin 2021) and play neutral-site games."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS venues (
            venue_id          INTEGER NOT NULL,
            name              TEXT NOT NULL,
            slug              TEXT,
            city              TEXT,
            state             TEXT,
            latitude          DOUBLE,
            longitude         DOUBLE,
            timezone          TEXT,
            altitude_ft       INTEGER,
            roof              TEXT,
            cf_bearing_deg    DOUBLE,
            orientation_source TEXT,
            left_line_ft      INTEGER,
            center_ft         INTEGER,
            right_line_ft     INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="teams",
        kind=TableKind.DIMENSION,
        key=("team_id", "season"),
        ddl="""
        CREATE TABLE IF NOT EXISTS teams (
            team_id           INTEGER NOT NULL,
            season            INTEGER NOT NULL,
            name              TEXT NOT NULL,
            abbreviation      TEXT,
            league            TEXT,
            division          TEXT,
            home_venue_id     INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="players",
        kind=TableKind.DIMENSION,
        key=("player_id",),
        ddl="""
        CREATE TABLE IF NOT EXISTS players (
            player_id         INTEGER NOT NULL,
            full_name         TEXT,
            bats              TEXT,
            throws            TEXT,
            primary_position  TEXT,
            birth_date        DATE,
            mlb_debut_date    DATE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    # -----------------------------------------------------------------------
    # Facts (feature-readable, point-in-time)
    # -----------------------------------------------------------------------
    TableSpec(
        name="games",
        kind=TableKind.FACT,
        key=("game_pk",),
        event_ts_column="scheduled_start_ts",
        notes=(
            "game_date_local is the slate date in PARK-local time, not UTC. A "
            "late Pacific first pitch is the next UTC day; keying on the UTC "
            "date would file it under the wrong slate."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS games (
            game_pk             BIGINT NOT NULL,
            season              INTEGER NOT NULL,
            game_type           TEXT NOT NULL,
            game_date_local     DATE NOT NULL,
            scheduled_start_ts  TIMESTAMPTZ NOT NULL,
            status              TEXT,
            status_code         TEXT,
            home_team_id        INTEGER NOT NULL,
            away_team_id        INTEGER NOT NULL,
            venue_id            INTEGER,
            doubleheader        TEXT,
            game_number         INTEGER,
            series_game_number  INTEGER,
            games_in_series     INTEGER,
            day_night           TEXT,
            scheduled_innings   INTEGER,
            resume_of_game_pk   BIGINT,
            as_of_ts            TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="probable_pitchers",
        kind=TableKind.FACT,
        key=("game_pk", "side"),
        notes="Probables change. Every observation is retained; the reader picks the latest as-of.",
        ddl="""
        CREATE TABLE IF NOT EXISTS probable_pitchers (
            game_pk           BIGINT NOT NULL,
            side              TEXT NOT NULL,
            pitcher_id        INTEGER,
            pitcher_name      TEXT,
            throws            TEXT,
            is_confirmed      BOOLEAN DEFAULT FALSE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="lineup_slots",
        kind=TableKind.FACT,
        key=("game_pk", "side", "batting_order"),
        notes=(
            "The most time-sensitive input in the system. is_confirmed "
            "distinguishes an official card from a projection; a price built "
            "on a projected lineup must be wider than one built on a confirmed "
            "card, so the flag has to survive into the model."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS lineup_slots (
            game_pk           BIGINT NOT NULL,
            side              TEXT NOT NULL,
            batting_order     INTEGER NOT NULL,
            player_id         INTEGER,
            player_name       TEXT,
            position          TEXT,
            bats              TEXT,
            is_confirmed      BOOLEAN NOT NULL DEFAULT FALSE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="statcast_pitches",
        kind=TableKind.FACT,
        key=("game_pk", "at_bat_number", "pitch_number"),
        dynamic_columns=True,
        required_columns=(
            "game_pk", "game_date", "at_bat_number", "pitch_number", "inning",
            "inning_topbot", "batter", "pitcher", "stand", "p_throws", "events",
            "description", "type", "zone", "balls", "strikes", "outs_when_up",
            "on_1b", "on_2b", "on_3b", "pitch_type", "release_speed", "plate_x",
            "plate_z", "sz_top", "sz_bot", "launch_speed", "launch_angle",
            "estimated_woba_using_speedangle", "woba_value", "woba_denom",
            "delta_run_exp", "bat_score", "fld_score", "bb_type",
        ),
        notes=(
            "Savant revises history, including closed seasons. Rows are "
            "versioned by as_of_ts and read point-in-time. Columns beyond the "
            "required set are added dynamically at load time rather than "
            "declared, so an upstream column addition is not a code change."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS statcast_pitches (
            game_pk           BIGINT NOT NULL,
            game_date         DATE NOT NULL,
            at_bat_number     INTEGER NOT NULL,
            pitch_number      INTEGER NOT NULL,
            inning            INTEGER,
            inning_topbot     TEXT,
            batter            INTEGER,
            pitcher           INTEGER,
            stand             TEXT,
            p_throws          TEXT,
            events            TEXT,
            description       TEXT,
            type              TEXT,
            zone              INTEGER,
            balls             INTEGER,
            strikes           INTEGER,
            outs_when_up      INTEGER,
            on_1b             INTEGER,
            on_2b             INTEGER,
            on_3b             INTEGER,
            pitch_type        TEXT,
            release_speed     DOUBLE,
            plate_x           DOUBLE,
            plate_z           DOUBLE,
            sz_top            DOUBLE,
            sz_bot            DOUBLE,
            launch_speed      DOUBLE,
            launch_angle      DOUBLE,
            estimated_woba_using_speedangle DOUBLE,
            woba_value        DOUBLE,
            woba_denom        DOUBLE,
            delta_run_exp     DOUBLE,
            bat_score         INTEGER,
            fld_score         INTEGER,
            bb_type           TEXT,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="pa_outcomes",
        kind=TableKind.FACT,
        key=("game_pk", "at_bat_number"),
        notes=(
            "One row per plate appearance, collapsed from pitch-level Statcast. "
            "The unit the projector and the simulator both work in.\n\n"
            "Batted-ball columns are carried alongside the realised outcome on "
            "purpose: exit velocity and launch angle are what the projector "
            "actually leans on, because contact quality stabilises far faster "
            "than the hits that happened to fall in."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS pa_outcomes (
            game_pk           BIGINT NOT NULL,
            at_bat_number     INTEGER NOT NULL,
            game_date         DATE NOT NULL,
            season            INTEGER NOT NULL,
            batter_id         INTEGER,
            pitcher_id        INTEGER,
            bat_side          TEXT,
            pit_throws        TEXT,
            inning            INTEGER,
            outcome           TEXT NOT NULL,
            is_intentional_bb BOOLEAN DEFAULT FALSE,
            launch_speed      DOUBLE,
            launch_angle      DOUBLE,
            bb_type           TEXT,
            xwoba_con         DOUBLE,
            pitches           INTEGER,
            swings            INTEGER,
            whiffs            INTEGER,
            called_strikes    INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="battedball_lookup",
        kind=TableKind.FACT,
        key=("through_date", "ev_bucket", "la_bucket"),
        notes=(
            "League-wide P(outcome | exit velocity, launch angle), fit on "
            "batted balls strictly before through_date.\n\n"
            "This is what turns contact quality into a hit-type distribution "
            "without using the batter's own realised hits, which is the whole "
            "point: a .380 BABIP on 200 balls in play is mostly the defence "
            "and the ballpark, not the hitter."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS battedball_lookup (
            through_date      DATE NOT NULL,
            ev_bucket         INTEGER NOT NULL,
            la_bucket         INTEGER NOT NULL,
            n                 BIGINT NOT NULL,
            p_1b              DOUBLE,
            p_2b              DOUBLE,
            p_3b              DOUBLE,
            p_hr              DOUBLE,
            p_out             DOUBLE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="pa_rates",
        kind=TableKind.FACT,
        key=("system", "player_id", "player_type", "vs_hand", "through_date"),
        notes=(
            "The projector's output, and the simulator's direct input: a "
            "multinomial over the eight PA outcomes.\n\n"
            "n_effective is the Dirichlet concentration, not decoration. The "
            "simulator draws rates from Dirichlet(n_effective * p) per "
            "simulation, so a player with 40 PA of history produces a genuinely "
            "wider game distribution than one with 4000 rather than a "
            "falsely confident point estimate.\n\n"
            "system is part of the key so the in-house projector and a "
            "vendor projection can be stored side by side and scored against "
            "each other after a season."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS pa_rates (
            system            TEXT NOT NULL,
            player_id         INTEGER NOT NULL,
            player_type       TEXT NOT NULL,
            vs_hand           TEXT NOT NULL,
            through_date      DATE NOT NULL,
            p_k               DOUBLE NOT NULL,
            p_bb              DOUBLE NOT NULL,
            p_hbp             DOUBLE NOT NULL,
            p_1b              DOUBLE NOT NULL,
            p_2b              DOUBLE NOT NULL,
            p_3b              DOUBLE NOT NULL,
            p_hr              DOUBLE NOT NULL,
            p_out             DOUBLE NOT NULL,
            n_observed        DOUBLE,
            n_effective       DOUBLE,
            prior_weight      DOUBLE,
            prior_cell        TEXT,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="projector_constants",
        kind=TableKind.FACT,
        key=("system", "player_type", "through_date", "bucket", "min_trials"),
        notes=(
            "The regression constants the projector fit at each snapshot.\n\n"
            "Persisted rather than left in a log line for two reasons: the "
            "pre-registered gate needs a machine-readable fitted k, and how k "
            "moves across snapshots is itself diagnostic -- a constant that "
            "lurches between weeks means the fit is unstable, whatever the "
            "projections look like.\n\n"
            "min_trials is part of the key because the same snapshot is fit at "
            "several playing-time thresholds. Only is_primary is used for "
            "shrinkage; the others exist so that a missed ratio can be "
            "diagnosed as sample composition rather than mis-specification -- "
            "published talent spreads are measured on qualified hitters, and "
            "fitting across everyone pulls k down."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS projector_constants (
            system            TEXT NOT NULL,
            player_type       TEXT NOT NULL,
            through_date      DATE NOT NULL,
            bucket            TEXT NOT NULL,
            min_trials        DOUBLE NOT NULL,
            is_primary        BOOLEAN NOT NULL DEFAULT FALSE,
            k                 DOUBLE NOT NULL,
            prior_mean        DOUBLE,
            var_observed      DOUBLE,
            var_binomial      DOUBLE,
            var_true          DOUBLE,
            saturated         BOOLEAN,
            n_players         INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="projections",
        kind=TableKind.FACT,
        key=("system", "player_id", "player_type", "snapshot_date"),
        notes=(
            "Daily snapshots. Projections are the true-talent base precisely "
            "because the regression is already done properly; season-to-date "
            "rates on 200 PA are mostly noise. Snapshotting daily is what makes "
            "a backtest able to ask what the projection said THAT day."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS projections (
            system            TEXT NOT NULL,
            player_id         INTEGER NOT NULL,
            player_type       TEXT NOT NULL,
            snapshot_date     DATE NOT NULL,
            player_name       TEXT,
            team              TEXT,
            pa                DOUBLE,
            ab                DOUBLE,
            ip                DOUBLE,
            tbf               DOUBLE,
            k                 DOUBLE,
            bb                DOUBLE,
            hbp               DOUBLE,
            singles           DOUBLE,
            doubles           DOUBLE,
            triples           DOUBLE,
            hr                DOUBLE,
            woba              DOUBLE,
            wrc_plus          DOUBLE,
            era               DOUBLE,
            fip               DOUBLE,
            k_pct             DOUBLE,
            bb_pct            DOUBLE,
            gb_pct            DOUBLE,
            fb_pct            DOUBLE,
            extras            JSON,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="retrosheet_events",
        kind=TableKind.FACT,
        key=("retro_game_id", "event_num"),
        notes=(
            "Source for empirical base-out transition and baserunner "
            "advancement matrices. Deliberately deeper history than the "
            "Statcast window: advancement rates move far more slowly than "
            "hitter talent, so more seasons buys precision without much bias."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS retrosheet_events (
            retro_game_id     TEXT NOT NULL,
            event_num         INTEGER NOT NULL,
            game_pk           BIGINT,
            season            INTEGER NOT NULL,
            game_date         DATE,
            park_id           TEXT,
            inning            INTEGER,
            bat_home_id       INTEGER,
            batter_retro_id   TEXT,
            pitcher_retro_id  TEXT,
            bat_hand          TEXT,
            pit_hand          TEXT,
            outs_before       INTEGER,
            start_base_state  INTEGER,
            end_base_state    INTEGER,
            event_code        INTEGER,
            event_text        TEXT,
            runs_on_play      INTEGER,
            outs_on_play      INTEGER,
            rbi               INTEGER,
            run1_dest         INTEGER,
            run2_dest         INTEGER,
            run3_dest         INTEGER,
            batter_dest       INTEGER,
            sb_flags          TEXT,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="weather_hourly",
        kind=TableKind.FACT,
        key=("venue_id", "valid_ts"),
        event_ts_column="valid_ts",
        notes=(
            "as_of_ts is the forecast ISSUE time, valid_ts is the hour it "
            "describes. Confusing the two is a leak: the 6pm actual is not "
            "something the 9am forecast knew."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS weather_hourly (
            venue_id            INTEGER NOT NULL,
            valid_ts            TIMESTAMPTZ NOT NULL,
            is_observation      BOOLEAN NOT NULL DEFAULT FALSE,
            temperature_f       DOUBLE,
            relative_humidity   DOUBLE,
            surface_pressure_hpa DOUBLE,
            wind_speed_mph      DOUBLE,
            wind_direction_deg  DOUBLE,
            wind_out_to_cf_mph  DOUBLE,
            wind_cross_lf_rf_mph DOUBLE,
            precipitation_prob  DOUBLE,
            cloud_cover_pct     DOUBLE,
            roof_closed         BOOLEAN,
            as_of_ts            TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="umpire_assignments",
        kind=TableKind.FACT,
        key=("game_pk", "role"),
        ddl="""
        CREATE TABLE IF NOT EXISTS umpire_assignments (
            game_pk           BIGINT NOT NULL,
            role              TEXT NOT NULL,
            umpire_id         INTEGER,
            umpire_name       TEXT,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="umpire_ratings",
        kind=TableKind.FACT,
        key=("umpire_id", "through_date"),
        notes=(
            "Derived in-house from Statcast called pitches with an expanding "
            "window, shrunk toward league average. through_date is exclusive: "
            "a rating dated D uses pitches strictly before D."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS umpire_ratings (
            umpire_id             INTEGER NOT NULL,
            through_date          DATE NOT NULL,
            called_pitches        BIGINT,
            called_strike_rate    DOUBLE,
            called_strike_rate_oe DOUBLE,
            k_pct_delta           DOUBLE,
            bb_pct_delta          DOUBLE,
            runs_per_game_delta   DOUBLE,
            shrinkage_weight      DOUBLE,
            as_of_ts              TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="pitcher_appearances",
        kind=TableKind.FACT,
        key=("game_pk", "player_id"),
        notes=(
            "Bullpen availability. as_of_ts is the END of the appearance, so a "
            "point-in-time read on today's slate sees yesterday's usage and not "
            "today's. This is what makes back-to-back and three-in-four "
            "constraints computable without leaking."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS pitcher_appearances (
            game_pk           BIGINT NOT NULL,
            player_id         INTEGER NOT NULL,
            team_id           INTEGER,
            game_date_local   DATE NOT NULL,
            is_start          BOOLEAN,
            pitches_thrown    INTEGER,
            batters_faced     INTEGER,
            outs_recorded     INTEGER,
            entered_inning    INTEGER,
            leverage_index    DOUBLE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="transactions",
        kind=TableKind.FACT,
        key=("transaction_id",),
        ddl="""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id    BIGINT NOT NULL,
            player_id         INTEGER,
            team_id           INTEGER,
            type_code         TEXT,
            description       TEXT,
            effective_date    DATE,
            resolution_date   DATE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="odds_snapshots",
        kind=TableKind.FACT,
        key=("book", "game_pk", "market_type", "line", "side", "as_of_ts"),
        notes=(
            "as_of_ts IS the snapshot time. A decision made at T may only read "
            "rows with as_of_ts <= T. The closing snapshot lives in a separate "
            "table so that it is structurally impossible to reach from here."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            book              TEXT NOT NULL,
            game_pk           BIGINT NOT NULL,
            market_type       TEXT NOT NULL,
            line              DOUBLE,
            side              TEXT NOT NULL,
            price_american    INTEGER,
            price_decimal     DOUBLE,
            implied_prob_raw  DOUBLE,
            book_event_id     TEXT,
            last_update_ts    TIMESTAMPTZ,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="market_quotes",
        kind=TableKind.FACT,
        key=("venue", "game_pk", "market_type", "line", "side", "quote_source", "as_of_ts"),
        notes=(
            "Order-book venues. Depth is stored because it is the difference "
            "between a tradeable quote and a decoration: a 1-lot bid at 55 "
            "against 500 offered at 58 does not mean fair value is 56.5.\n\n"
            "quote_source is part of the key because a venue's market summary "
            "and its order book are two different observations of the same "
            "market in the same tick: the summary carries top-of-book with no "
            "sizes, the book carries depth. Keying without it deduplicates the "
            "book away and silently discards the only reason to poll it."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS market_quotes (
            venue             TEXT NOT NULL,
            game_pk           BIGINT NOT NULL,
            market_type       TEXT NOT NULL,
            line              DOUBLE,
            side              TEXT NOT NULL,
            quote_source      TEXT NOT NULL DEFAULT 'summary',
            venue_ticker      TEXT,
            best_bid          DOUBLE,
            best_ask          DOUBLE,
            bid_size          BIGINT,
            ask_size          BIGINT,
            depth_bid_json    JSON,
            depth_ask_json    JSON,
            last_trade_price  DOUBLE,
            volume            BIGINT,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    # -----------------------------------------------------------------------
    # Outcomes -- quarantined from the ordinary feature reader
    # -----------------------------------------------------------------------
    TableSpec(
        name="game_results",
        kind=TableKind.OUTCOME,
        key=("game_pk",),
        notes=(
            "as_of_ts is when the game went final. Reachable only via an "
            "explicit allow_outcomes=True, which exists so that every read of "
            "the labels is greppable. Derived history features (team form, "
            "bullpen burn) belong in a FACT table built by an expanding-window "
            "job, not in ad-hoc reads of this one."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS game_results (
            game_pk           BIGINT NOT NULL,
            home_runs         INTEGER,
            away_runs         INTEGER,
            home_runs_f5      INTEGER,
            away_runs_f5      INTEGER,
            innings_played    DOUBLE,
            home_won          BOOLEAN,
            went_extras       BOOLEAN,
            home_half_9_played BOOLEAN,
            status_code       TEXT,
            is_final          BOOLEAN,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="pitcher_game_stats",
        kind=TableKind.OUTCOME,
        key=("game_pk", "player_id"),
        ddl="""
        CREATE TABLE IF NOT EXISTS pitcher_game_stats (
            game_pk           BIGINT NOT NULL,
            player_id         INTEGER NOT NULL,
            team_id           INTEGER,
            is_start          BOOLEAN,
            outs_recorded     INTEGER,
            batters_faced     INTEGER,
            pitches_thrown    INTEGER,
            strikeouts        INTEGER,
            walks             INTEGER,
            hits_allowed      INTEGER,
            home_runs_allowed INTEGER,
            earned_runs       INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    TableSpec(
        name="batter_game_stats",
        kind=TableKind.OUTCOME,
        key=("game_pk", "player_id"),
        ddl="""
        CREATE TABLE IF NOT EXISTS batter_game_stats (
            game_pk           BIGINT NOT NULL,
            player_id         INTEGER NOT NULL,
            team_id           INTEGER,
            batting_order     INTEGER,
            plate_appearances INTEGER,
            at_bats           INTEGER,
            hits              INTEGER,
            doubles           INTEGER,
            triples           INTEGER,
            home_runs         INTEGER,
            walks             INTEGER,
            strikeouts        INTEGER,
            runs              INTEGER,
            rbi               INTEGER,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
    # -----------------------------------------------------------------------
    # Closing lines -- evaluation only, never a feature under any flag
    # -----------------------------------------------------------------------
    TableSpec(
        name="closing_lines",
        kind=TableKind.CLOSING,
        key=("book", "game_pk", "market_type", "line", "side"),
        notes=(
            "The CLV yardstick. Physically separated from odds_snapshots so "
            "that 'accidentally trained on the close' is not a mistake anyone "
            "can make by forgetting a WHERE clause. Only market/clv.py reads it."
        ),
        ddl="""
        CREATE TABLE IF NOT EXISTS closing_lines (
            book              TEXT NOT NULL,
            game_pk           BIGINT NOT NULL,
            market_type       TEXT NOT NULL,
            line              DOUBLE,
            side              TEXT NOT NULL,
            price_american    INTEGER,
            price_decimal     DOUBLE,
            implied_prob_raw  DOUBLE,
            novig_prob        DOUBLE,
            devig_method      TEXT,
            captured_ts       TIMESTAMPTZ NOT NULL,
            minutes_to_first_pitch DOUBLE,
            as_of_ts          TIMESTAMPTZ NOT NULL,
            """ + _PROVENANCE + """
        )
        """,
    ),
)

BY_NAME: dict[str, TableSpec] = {spec.name: spec for spec in TABLES}


def get(name: str) -> TableSpec:
    if name not in BY_NAME:
        raise KeyError(f"unknown table '{name}'. Known: {sorted(BY_NAME)}")
    return BY_NAME[name]


def feature_readable_tables() -> list[TableSpec]:
    return [spec for spec in TABLES if spec.feature_readable]


def quarantined_tables() -> list[TableSpec]:
    return [spec for spec in TABLES if spec.quarantined]


def create_all(con: duckdb.DuckDBPyConnection) -> None:
    """Create every registered table. Idempotent."""
    for spec in TABLES:
        con.execute(spec.ddl)


def actual_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [row[0] for row in rows]
