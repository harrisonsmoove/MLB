"""Point-in-time reads.

Ground rule 1: *every feature must be computable from data that existed before
first pitch.* This module is the only sanctioned way to read the warehouse when
building features, and it is written so that the leaky query is the one that is
hard to write.

Three structural guards, not conventions:

* ``as_of()`` always requires an explicit timestamp. There is no overload that
  reads "current" state, because that overload is how a backtest quietly reads
  tomorrow's data.
* ``OUTCOME`` tables (the labels) require ``allow_outcomes=True``. The flag does
  nothing but make every read of the labels greppable in review.
* ``CLOSING`` tables are unreachable here at any flag. The closing line is the
  yardstick we are measured against; a feature built on it would make the
  measurement meaningless. Only :func:`closing_lines_for_clv` reads them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import polars as pl

from mlb_edge.storage import schema
from mlb_edge.storage.schema import TableKind
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import ensure_utc


class LeakError(RuntimeError):
    """Raised when a read would expose data that did not exist at the as-of time."""


def _check_readable(spec: schema.TableSpec, allow_outcomes: bool) -> None:
    if spec.kind == TableKind.CLOSING:
        raise LeakError(
            f"'{spec.name}' holds closing prices and is not readable as a feature "
            "under any flag. The close is the benchmark; conditioning on it would "
            "make CLV self-referential. Use pit.closing_lines_for_clv() in the "
            "evaluator instead."
        )
    if spec.kind == TableKind.OUTCOME and not allow_outcomes:
        raise LeakError(
            f"'{spec.name}' holds realised outcomes (the labels). Pass "
            "allow_outcomes=True if this is a settlement or label-assembly read. "
            "If you are building a feature, build a derived FACT table with an "
            "expanding window instead."
        )
    if spec.as_of_column is None:
        raise LeakError(
            f"'{spec.name}' has no as-of column ({spec.exempt_reason}) and cannot "
            "be read point-in-time."
        )


def as_of(
    wh: Warehouse,
    table: str,
    ts: datetime,
    *,
    columns: Sequence[str] | None = None,
    where: str | None = None,
    params: Sequence[Any] | None = None,
    latest_per_key: bool = True,
    inclusive: bool = True,
    allow_outcomes: bool = False,
) -> pl.DataFrame:
    """Rows as they were known at ``ts``.

    With ``latest_per_key`` (the default) this collapses the version history to
    one row per natural key -- the most recent observation at or before ``ts``.
    That is what "what did we believe then" means for a slowly-changing fact
    like a probable pitcher or a lineup card.

    Set ``latest_per_key=False`` for tables where every version is itself an
    observation to keep, such as an odds snapshot series.

    ``inclusive`` controls whether a row stamped exactly at ``ts`` is visible.
    It defaults to True (a decision made at T may use data published at T) but
    should be False when ``ts`` is a first-pitch time, since a lineup change
    stamped at first pitch was not actionable.
    """
    spec = schema.get(table)
    _check_readable(spec, allow_outcomes)
    cutoff = ensure_utc(ts)

    as_of_col = spec.as_of_column
    comparison = "<=" if inclusive else "<"
    select_cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"

    clauses = [f'"{as_of_col}" {comparison} ?']
    bind: list[Any] = [cutoff]
    if where:
        clauses.append(f"({where})")
        bind.extend(params or [])
    predicate = " AND ".join(clauses)

    if not latest_per_key:
        return wh.sql(
            f"SELECT {select_cols} FROM {table} WHERE {predicate}", bind
        )

    partition = ", ".join(f'"{c}"' for c in spec.key if c != as_of_col)
    if not partition:
        # Key is the as-of column alone; every row is its own version.
        return wh.sql(f"SELECT {select_cols} FROM {table} WHERE {predicate}", bind)

    query = f"""
        SELECT {select_cols} FROM (
            SELECT *, row_number() OVER (
                PARTITION BY {partition}
                ORDER BY "{as_of_col}" DESC
            ) AS _version_rank
            FROM {table}
            WHERE {predicate}
        )
        WHERE _version_rank = 1
    """
    frame = wh.sql(query, bind)
    if "_version_rank" in frame.columns:
        frame = frame.drop("_version_rank")
    return frame


def as_of_first_pitch(
    wh: Warehouse,
    table: str,
    game_pk: int,
    *,
    lead_seconds: float = 0.0,
    **kwargs: Any,
) -> pl.DataFrame:
    """Rows known strictly before a game's scheduled first pitch.

    ``lead_seconds`` pulls the cutoff earlier, which is how you model a decision
    made N seconds before the bell rather than at it.
    """
    row = wh.sql(
        "SELECT scheduled_start_ts FROM games WHERE game_pk = ? "
        "ORDER BY as_of_ts DESC LIMIT 1",
        [game_pk],
    )
    if row.is_empty():
        raise LeakError(f"game_pk {game_pk} is not in the games table; cannot bound its as-of.")
    start_ts = row["scheduled_start_ts"][0]
    cutoff = ensure_utc(start_ts)
    if lead_seconds:
        cutoff = cutoff.fromtimestamp(cutoff.timestamp() - lead_seconds, tz=cutoff.tzinfo)
    kwargs.setdefault("inclusive", False)
    kwargs.setdefault("where", "game_pk = ?")
    kwargs.setdefault("params", [game_pk])
    return as_of(wh, table, cutoff, **kwargs)


def snapshots_before(
    wh: Warehouse,
    table: str,
    ts: datetime,
    *,
    game_pk: int | None = None,
    book: str | None = None,
    market_type: str | None = None,
) -> pl.DataFrame:
    """Full odds/quote snapshot history up to ``ts``.

    Every snapshot is kept, not just the latest, because line *movement* is
    itself a feature and collapsing to the last observation would destroy it.
    """
    spec = schema.get(table)
    _check_readable(spec, allow_outcomes=False)

    clauses: list[str] = []
    params: list[Any] = []
    if game_pk is not None:
        clauses.append("game_pk = ?")
        params.append(game_pk)
    if book is not None:
        column = "venue" if table == "market_quotes" else "book"
        clauses.append(f"{column} = ?")
        params.append(book)
    if market_type is not None:
        clauses.append("market_type = ?")
        params.append(market_type)

    return as_of(
        wh,
        table,
        ts,
        where=" AND ".join(clauses) if clauses else None,
        params=params,
        latest_per_key=False,
    )


def expanding_aggregate(
    wh: Warehouse,
    *,
    table: str,
    group_by: Sequence[str],
    value_expr: str,
    through: date | datetime,
    date_column: str = "game_date_local",
    where: str | None = None,
    params: Sequence[Any] | None = None,
    allow_outcomes: bool = False,
) -> pl.DataFrame:
    """Season-to-date style aggregate over an expanding window.

    The window is half-open and *excludes* ``through``. A "season to date"
    number computed for a game on day D must not include day D -- that is the
    single most common way a baseball feature ends up containing its own label.
    """
    spec = schema.get(table)
    _check_readable(spec, allow_outcomes)

    clauses = [f'"{date_column}" < ?']
    bind: list[Any] = [through]
    if where:
        clauses.append(f"({where})")
        bind.extend(params or [])

    groups = ", ".join(f'"{c}"' for c in group_by)
    return wh.sql(
        f"""
        SELECT {groups}, {value_expr}
        FROM {table}
        WHERE {" AND ".join(clauses)}
        GROUP BY {groups}
        """,
        bind,
    )


def closing_lines_for_clv(
    wh: Warehouse,
    *,
    game_pks: Sequence[int] | None = None,
    book: str | None = None,
    market_type: str | None = None,
) -> pl.DataFrame:
    """The only sanctioned read of closing prices.

    Named for its single legitimate caller. If you find yourself importing this
    from anywhere under ``features/`` or ``model/``, that is the bug.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if game_pks is not None:
        placeholders = ", ".join("?" for _ in game_pks)
        clauses.append(f"game_pk IN ({placeholders})")
        params.extend(game_pks)
    if book is not None:
        clauses.append("book = ?")
        params.append(book)
    if market_type is not None:
        clauses.append("market_type = ?")
        params.append(market_type)
    predicate = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return wh.sql(f"SELECT * FROM closing_lines {predicate}", params)


def label_frame(wh: Warehouse, game_pks: Sequence[int] | None = None) -> pl.DataFrame:
    """Realised game outcomes, for training labels and settlement.

    Deliberately a named function rather than a generic read: assembling labels
    is a legitimate operation, and giving it one obvious front door means the
    ad-hoc outcome reads stand out in review.
    """
    if game_pks is None:
        return wh.sql("SELECT * FROM game_results WHERE is_final")
    placeholders = ", ".join("?" for _ in game_pks)
    return wh.sql(
        f"SELECT * FROM game_results WHERE is_final AND game_pk IN ({placeholders})",
        list(game_pks),
    )
