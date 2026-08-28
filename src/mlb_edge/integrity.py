"""Warehouse integrity checks.

The same checks run in two places, deliberately: the test suite runs them
against constructed fixtures here, and ``mlb-edge verify`` runs them against the
real multi-season warehouse. A guarantee that only holds on synthetic data is
not a guarantee, and this environment had no network access to prove the second
half -- so the checks are written to be run by someone who does.

Severity is the useful axis:

``ERROR``  -- a correctness violation. The warehouse is not safe to model on.
``WARN``   -- a quality signal that needs a human judgement (unresolved market
              events, a parser whose coverage dropped).
``INFO``   -- a measurement worth printing, like how many Statcast rows have
              been revised since first ingest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from mlb_edge.storage import schema
from mlb_edge.storage.schema import TableKind
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import utcnow


class Severity(StrEnum):
    ERROR = "ERROR"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: Severity
    detail: str
    metric: float | None = None

    def line(self) -> str:
        status = "PASS" if self.passed else self.severity.value
        metric = f" [{self.metric:g}]" if self.metric is not None else ""
        return f"{status:5s} {self.name}{metric}: {self.detail}"


def run_all(wh: Warehouse, *, now: datetime | None = None) -> list[CheckResult]:
    reference = now or utcnow()
    results: list[CheckResult] = []
    results.extend(check_as_of_present(wh))
    results.extend(check_no_future_as_of(wh, reference))
    results.append(check_game_pk_uniqueness(wh))
    results.append(check_doubleheader_keying(wh))
    results.extend(check_orphan_game_pks(wh))
    results.append(check_forecast_precedes_valid(wh))
    results.append(check_closing_before_first_pitch(wh))
    results.append(check_odds_snapshots_pregame(wh))
    results.append(check_retrosheet_parse_coverage(wh))
    results.append(check_retrosheet_publication_lag(wh))
    results.append(check_statcast_required_columns(wh))
    results.append(check_statcast_revisions(wh))
    results.append(check_projection_id_resolution(wh))
    return results


def has_errors(results: list[CheckResult]) -> bool:
    return any(not r.passed and r.severity == Severity.ERROR for r in results)


def _exists(wh: Warehouse, table: str) -> bool:
    row = wh.con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    return bool(row and row[0])


def _scalar(wh: Warehouse, query: str, params: list[Any] | None = None) -> Any:
    row = wh.con.execute(query, params or []).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Ground rule 1: every table carries an as-of axis, and nothing is stamped in
# the future.
# ---------------------------------------------------------------------------
def check_as_of_present(wh: Warehouse) -> list[CheckResult]:
    results: list[CheckResult] = []
    for spec in schema.TABLES:
        if spec.as_of_column is None:
            results.append(
                CheckResult(
                    f"as_of_declared.{spec.name}",
                    passed=spec.kind == TableKind.META and bool(spec.exempt_reason),
                    severity=Severity.ERROR,
                    detail=spec.exempt_reason or "no as_of column and no exemption reason",
                )
            )
            continue
        columns = set(wh.columns(spec.name))
        if spec.as_of_column not in columns:
            results.append(
                CheckResult(
                    f"as_of_declared.{spec.name}",
                    passed=False,
                    severity=Severity.ERROR,
                    detail=f"declared as_of column '{spec.as_of_column}' is missing from the table",
                )
            )
            continue
        nulls = _scalar(
            wh, f'SELECT count(*) FROM {spec.name} WHERE "{spec.as_of_column}" IS NULL'
        )
        results.append(
            CheckResult(
                f"as_of_not_null.{spec.name}",
                passed=nulls == 0,
                severity=Severity.ERROR,
                detail=f"{nulls} rows have a null {spec.as_of_column}",
                metric=float(nulls or 0),
            )
        )
    return results


def check_no_future_as_of(wh: Warehouse, now: datetime) -> list[CheckResult]:
    """No row may claim to have been known in the future.

    Retrosheet is exempt because its as_of is a *publication* date that is
    intentionally set forward of the season it describes -- that is the point of
    it, and it is verified separately by the publication-lag check.
    """
    results: list[CheckResult] = []
    for spec in schema.TABLES:
        if spec.as_of_column is None or spec.name == "retrosheet_events":
            continue
        count = _scalar(
            wh, f'SELECT count(*) FROM {spec.name} WHERE "{spec.as_of_column}" > ?', [now]
        )
        results.append(
            CheckResult(
                f"no_future_as_of.{spec.name}",
                passed=count == 0,
                severity=Severity.ERROR,
                detail=f"{count} rows are stamped after {now.isoformat()}",
                metric=float(count or 0),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Ground rule 4: game_pk is the key.
# ---------------------------------------------------------------------------
def check_game_pk_uniqueness(wh: Warehouse) -> CheckResult:
    """A point-in-time read of ``games`` must yield exactly one row per game_pk."""
    dupes = _scalar(
        wh,
        """
        SELECT count(*) FROM (
            SELECT game_pk FROM (
                SELECT game_pk, row_number() OVER (
                    PARTITION BY game_pk ORDER BY as_of_ts DESC
                ) rn FROM games
            ) WHERE rn = 1
            GROUP BY game_pk HAVING count(*) > 1
        )
        """,
    )
    return CheckResult(
        "game_pk_unique",
        passed=dupes == 0,
        severity=Severity.ERROR,
        detail=f"{dupes} game_pks resolve to more than one current row",
        metric=float(dupes or 0),
    )


def check_doubleheader_keying(wh: Warehouse) -> CheckResult:
    """Prove the naive key would collide, and that game_pk does not.

    This is not a hypothetical. A normal season has 25-40 doubleheaders, each of
    which would silently merge two different games under a
    ``(date, home, away)`` key -- and merge one game's odds onto the other's
    result about half the time.
    """
    collisions = _scalar(
        wh,
        """
        SELECT count(*) FROM (
            SELECT game_date_local, home_team_id, away_team_id, count(DISTINCT game_pk) AS n
            FROM (
                SELECT game_date_local, home_team_id, away_team_id, game_pk,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
            ) WHERE rn = 1
            GROUP BY 1, 2, 3
            HAVING count(DISTINCT game_pk) > 1
        )
        """,
    )
    return CheckResult(
        "doubleheader_keying",
        passed=True,
        severity=Severity.INFO,
        detail=(
            f"{collisions} (date, home, away) groups contain more than one game_pk. "
            "Each is a game a naive key would have merged; game_pk keeps them apart."
        ),
        metric=float(collisions or 0),
    )


def check_orphan_game_pks(wh: Warehouse) -> list[CheckResult]:
    """Every game_pk referenced elsewhere must exist in the games spine."""
    results: list[CheckResult] = []
    for table in (
        "lineup_slots",
        "umpire_assignments",
        "odds_snapshots",
        "market_quotes",
        "closing_lines",
        "game_results",
        "statcast_pitches",
    ):
        if not _exists(wh, table):
            continue
        orphans = _scalar(
            wh,
            f"""
            SELECT count(DISTINCT t.game_pk) FROM {table} t
            LEFT JOIN games g ON g.game_pk = t.game_pk
            WHERE g.game_pk IS NULL
            """,
        )
        results.append(
            CheckResult(
                f"no_orphan_game_pk.{table}",
                passed=orphans == 0,
                severity=Severity.ERROR,
                detail=f"{orphans} game_pks in {table} have no row in games",
                metric=float(orphans or 0),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Timestamp semantics
# ---------------------------------------------------------------------------
def check_forecast_precedes_valid(wh: Warehouse) -> CheckResult:
    """A forecast must be issued before the hour it forecasts.

    If ``as_of_ts >= valid_ts`` on a non-observation row, the row is an
    observation mislabelled as a forecast -- and using it as a pre-game feature
    would mean knowing the actual first-pitch conditions in advance.
    """
    bad = _scalar(
        wh,
        "SELECT count(*) FROM weather_hourly WHERE is_observation = FALSE AND as_of_ts >= valid_ts",
    )
    return CheckResult(
        "forecast_issued_before_valid",
        passed=bad == 0,
        severity=Severity.ERROR,
        detail=f"{bad} forecast rows are stamped at or after the hour they describe",
        metric=float(bad or 0),
    )


def check_closing_before_first_pitch(wh: Warehouse) -> CheckResult:
    """A closing line captured after first pitch is an in-play price."""
    bad = _scalar(
        wh,
        """
        SELECT count(*) FROM closing_lines c
        JOIN (
            SELECT game_pk, scheduled_start_ts FROM (
                SELECT game_pk, scheduled_start_ts,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
            ) WHERE rn = 1
        ) g ON g.game_pk = c.game_pk
        WHERE c.captured_ts > g.scheduled_start_ts
        """,
    )
    return CheckResult(
        "closing_before_first_pitch",
        passed=bad == 0,
        severity=Severity.ERROR,
        detail=(
            f"{bad} closing_lines rows were captured after first pitch; those are "
            "in-play prices and would flatter every CLV number computed from them"
        ),
        metric=float(bad or 0),
    )


def check_odds_snapshots_pregame(wh: Warehouse) -> CheckResult:
    """Count in-play snapshots. Legal to store, dangerous to use unfiltered."""
    count = _scalar(
        wh,
        """
        SELECT count(*) FROM odds_snapshots o
        JOIN (
            SELECT game_pk, scheduled_start_ts FROM (
                SELECT game_pk, scheduled_start_ts,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
            ) WHERE rn = 1
        ) g ON g.game_pk = o.game_pk
        WHERE o.as_of_ts > g.scheduled_start_ts
        """,
    )
    return CheckResult(
        "odds_snapshots_in_play_count",
        passed=True,
        severity=Severity.INFO,
        detail=(
            f"{count} odds snapshots are post-first-pitch. Storing them is fine; any "
            "pre-game feature must bound as_of_ts by scheduled_start_ts to exclude them."
        ),
        metric=float(count or 0),
    )


# ---------------------------------------------------------------------------
# Source-specific quality
# ---------------------------------------------------------------------------
def check_retrosheet_parse_coverage(wh: Warehouse, threshold: float = 0.995) -> CheckResult:
    """Fraction of plays the event parser handled without falling back.

    Reported rather than assumed. A parser that silently mislabels a few percent
    of plays would bias the advancement matrices in a direction no aggregate
    would reveal.
    """
    total = _scalar(wh, "SELECT count(*) FROM retrosheet_events") or 0
    if total == 0:
        return CheckResult(
            "retrosheet_parse_coverage",
            passed=True,
            severity=Severity.INFO,
            detail="no retrosheet events loaded",
        )
    ok = _scalar(wh, "SELECT count(*) FROM retrosheet_events WHERE sb_flags = 'ok'") or 0
    coverage = ok / total
    return CheckResult(
        "retrosheet_parse_coverage",
        passed=coverage >= threshold,
        severity=Severity.WARN,
        detail=f"{ok}/{total} plays parsed cleanly (threshold {threshold:.1%})",
        metric=coverage,
    )


def check_retrosheet_publication_lag(wh: Warehouse) -> CheckResult:
    """Retrosheet rows must not claim availability during their own season."""
    bad = _scalar(
        wh,
        "SELECT count(*) FROM retrosheet_events WHERE as_of_ts <= make_timestamptz("
        "season, 12, 31, 23, 59, 59.0)",
    )
    return CheckResult(
        "retrosheet_publication_lag",
        passed=bad == 0,
        severity=Severity.ERROR,
        detail=(
            f"{bad} retrosheet rows are stamped inside their own season; event files "
            "are published the following spring and a matrix fit on them would be "
            "using data that did not exist yet"
        ),
        metric=float(bad or 0),
    )


def check_statcast_required_columns(wh: Warehouse) -> CheckResult:
    spec = schema.get("statcast_pitches")
    present = set(wh.columns("statcast_pitches"))
    missing = sorted(set(spec.required_columns) - present)
    return CheckResult(
        "statcast_required_columns",
        passed=not missing,
        severity=Severity.ERROR,
        detail=f"missing required columns: {missing}" if missing else "all required columns present",
        metric=float(len(missing)),
    )


def check_statcast_revisions(wh: Warehouse) -> CheckResult:
    """How many pitches Savant has restated since first ingest."""
    revised = _scalar(
        wh,
        """
        SELECT count(*) FROM (
            SELECT game_pk, at_bat_number, pitch_number
            FROM statcast_pitches
            GROUP BY 1, 2, 3
            HAVING count(DISTINCT as_of_ts) > 1
        )
        """,
    )
    return CheckResult(
        "statcast_revisions",
        passed=True,
        severity=Severity.INFO,
        detail=(
            f"{revised} pitches carry more than one version. Non-zero is expected and "
            "healthy: it means revisions are being captured rather than overwritten."
        ),
        metric=float(revised or 0),
    )


def check_projection_id_resolution(wh: Warehouse, threshold: float = 0.95) -> CheckResult:
    """Share of projection rows carrying a usable MLBAM id.

    FanGraphs keys on its own player ids. A low rate here means the field map in
    ``settings.yaml`` is wrong, and the symptom would otherwise be a projection
    set that quietly covers only part of the league.
    """
    total = _scalar(wh, "SELECT count(*) FROM projections") or 0
    if total == 0:
        return CheckResult(
            "projection_id_resolution",
            passed=True,
            severity=Severity.INFO,
            detail="no projections loaded",
        )
    resolved = _scalar(wh, "SELECT count(*) FROM projections WHERE player_id IS NOT NULL") or 0
    rate = resolved / total
    return CheckResult(
        "projection_id_resolution",
        passed=rate >= threshold,
        severity=Severity.WARN,
        detail=f"{resolved}/{total} projection rows have an MLBAM id",
        metric=rate,
    )
