"""Sportsbook odds ingestion.

Provider-agnostic by construction: the adapter below speaks The Odds API, but
everything downstream sees only the ``odds_snapshots`` schema, so swapping
providers never touches the model.

Configured for **Tier 0** -- the free plan. That means moneylines only, a
handful of regions, no historical backfill, and a hard monthly request budget.
The consequence is worth stating plainly rather than discovering later: there is
no historical odds archive to backtest against on this tier. The archive starts
accumulating the day the poller first runs, and a usable CLV dataset is roughly
one season away. Everything else in the system can be validated sooner; CLV
cannot be rushed.

Closing lines are written to a *different table* than in-play snapshots. The
separation is physical rather than a flag, because "filter out the close" is a
WHERE clause someone eventually forgets.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from mlb_edge.http import BudgetExceeded
from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.ingest.matching import GameMatcher
from mlb_edge.market.prices import PriceError, american_to_decimal, implied_prob
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow


class TheOddsApiIngester(Ingester):
    source_name = "odds"
    writes_tables = ("odds_snapshots", "closing_lines")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._matcher: GameMatcher | None = None
        self.unresolved_events: list[str] = []

    @property
    def matcher(self) -> GameMatcher:
        if self._matcher is None:
            if self.warehouse is None:
                raise RuntimeError("odds ingestion needs a warehouse to resolve game_pk")
            self._matcher = GameMatcher(self.warehouse, self.settings)
        return self._matcher

    # -- budget --------------------------------------------------------------
    def requests_this_month(self, now: datetime | None = None) -> int:
        """Count this month's fetches from the raw manifest.

        The manifest is the honest counter: it records every payload actually
        retrieved, so it cannot drift from reality the way a separate tally
        would after a crash mid-run.
        """
        if self.warehouse is None:
            return 0
        reference = ensure_utc(now or utcnow())
        month_start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.warehouse.sql(
            "SELECT count(*) AS n FROM raw_manifest WHERE source = ? AND retrieved_at >= ?",
            [self.source_name, month_start],
        )
        return int(row["n"][0]) if not row.is_empty() else 0

    def check_budget(self, planned: int, now: datetime | None = None) -> None:
        budget = self.config.get("monthly_request_budget")
        if not budget:
            return
        used = self.requests_this_month(now)
        if used + planned > int(budget):
            raise BudgetExceeded(
                f"odds: {used} of {budget} monthly requests already used; {planned} more "
                "would exceed the free-tier budget. Raise monthly_request_budget only "
                "after confirming the plan actually allows it."
            )

    # -- planning ------------------------------------------------------------
    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        """One live-odds poll. Tier 0 has no historical endpoint.

        The date range is accepted for interface symmetry but ignored: the free
        plan returns only currently-open markets. Asking for a past range would
        silently return today's board, which is precisely the sort of quiet
        wrongness that produces a beautiful, meaningless backtest.
        """
        tier = int(self.config.get("tier", 0))
        regions = (self.config.get("regions_by_tier", {}) or {}).get(tier, ["us"])
        markets = (self.config.get("markets_by_tier", {}) or {}).get(tier, ["h2h"])
        snapshot_label = kwargs.get("label") or utcnow().strftime("%Y%m%dT%H%M%SZ")

        return [
            FetchTask(
                dataset="live_odds",
                partition=snapshot_label,
                url=self.config.endpoint("odds", sport=self.config.get("sport_key")),
                params={
                    "apiKey": self.config.require("api_key"),
                    "regions": ",".join(regions),
                    "markets": ",".join(markets),
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
                # Every poll is its own snapshot; a cached one is never reused.
                max_age_seconds=0.0,
                context={"is_closing": bool(kwargs.get("is_closing"))},
            )
        ]

    def run(self, start: date, end: date, **kwargs: Any) -> Any:
        if not kwargs.get("dry_run"):
            self.check_budget(planned=1)
        return super().run(start, end, **kwargs)

    # -- parsing -------------------------------------------------------------
    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        events = json.loads(payload)
        if not isinstance(events, list):
            return {}

        is_closing = bool(task.context.get("is_closing")) if task else False
        rows = self._rows_from_events(events, entry, is_closing=is_closing)
        frame = pl.DataFrame(rows) if rows else pl.DataFrame()
        return {"closing_lines" if is_closing else "odds_snapshots": frame}

    def _rows_from_events(
        self, events: list[Any], entry: RawEntry, *, is_closing: bool
    ) -> list[dict[str, Any]]:
        prov = provenance_columns(entry, self.source_name)
        as_of = entry.retrieved_ts
        rows: list[dict[str, Any]] = []

        for event in events:
            if not isinstance(event, dict):
                continue
            commence = event.get("commence_time")
            home = event.get("home_team")
            away = event.get("away_team")
            if not (commence and home and away):
                continue

            commence_ts = parse_iso_utc(commence)
            match = self.matcher.resolve(
                commence_ts=commence_ts, home_team_text=home, away_team_text=away
            )
            if not match.resolved:
                # Not dropped: the payload is in the immutable cache, so a
                # matcher fix plus reload_from_cache recovers every one of these.
                self.unresolved_events.append(f"{away} @ {home} {commence}: {match.reason}")
                continue

            for bookmaker in event.get("bookmakers", []) or []:
                book = bookmaker.get("key")
                if not book:
                    continue
                book_update = bookmaker.get("last_update")
                for market in bookmaker.get("markets", []) or []:
                    market_type = market.get("key")
                    for outcome in market.get("outcomes", []) or []:
                        row = self._outcome_row(
                            outcome,
                            book=book,
                            market_type=market_type,
                            game_pk=match.game_pk,
                            home=home,
                            book_event_id=str(event.get("id") or ""),
                            last_update=market.get("last_update") or book_update,
                            as_of=as_of,
                            prov=prov,
                        )
                        if row is None:
                            continue
                        if is_closing:
                            row = self._as_closing(row, commence_ts, as_of)
                        rows.append(row)
        return rows

    def _outcome_row(
        self,
        outcome: dict[str, Any],
        *,
        book: str,
        market_type: str | None,
        game_pk: int | None,
        home: str,
        book_event_id: str,
        last_update: str | None,
        as_of: datetime,
        prov: dict[str, Any],
    ) -> dict[str, Any] | None:
        price = outcome.get("price")
        if price is None or market_type is None:
            return None
        try:
            decimal_odds = american_to_decimal(price)
        except PriceError:
            return None

        name = str(outcome.get("name") or "")
        # Sides are stored as home/away/over/under rather than team names, so a
        # franchise rename upstream cannot orphan historical rows.
        is_total = market_type == "totals"
        side = name.lower() if is_total else ("home" if name == home else "away")

        return {
            "book": book,
            "game_pk": game_pk,
            "market_type": market_type,
            "line": _to_float(outcome.get("point")),
            "side": side,
            "price_american": int(price),
            "price_decimal": decimal_odds,
            "implied_prob_raw": implied_prob(decimal_odds),
            "book_event_id": book_event_id,
            "last_update_ts": parse_iso_utc(last_update) if last_update else None,
            "as_of_ts": as_of,
            **prov,
        }

    def _as_closing(
        self, row: dict[str, Any], commence_ts: datetime, as_of: datetime
    ) -> dict[str, Any]:
        closing = dict(row)
        closing.pop("book_event_id", None)
        closing.pop("last_update_ts", None)
        closing["captured_ts"] = as_of
        closing["minutes_to_first_pitch"] = (commence_ts - as_of).total_seconds() / 60.0
        # Devigging is market/devig.py's job and needs both sides of the market,
        # so it is applied as a later pass rather than guessed per outcome here.
        closing["novig_prob"] = None
        closing["devig_method"] = None
        return closing

    # -- closing capture -----------------------------------------------------
    def capture_closing(self, *, minutes_before: float = 5.0) -> Any:
        """Poll and write to ``closing_lines`` for games about to start."""
        now = utcnow()
        return self.run(
            now.date(),
            now.date(),
            label=f"closing_{now.strftime('%Y%m%dT%H%M%SZ')}",
            is_closing=True,
            force_refresh=True,
            window_minutes=minutes_before,
        )


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def games_closing_within(warehouse: Any, minutes: float, now: datetime | None = None) -> list[int]:
    """game_pks whose scheduled first pitch is inside the next ``minutes``."""
    reference = ensure_utc(now or utcnow())
    rows = warehouse.sql(
        """
        SELECT game_pk FROM (
            SELECT game_pk, scheduled_start_ts,
                   row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
            FROM games
        ) WHERE rn = 1 AND scheduled_start_ts BETWEEN ? AND ?
        """,
        [reference, reference + timedelta(minutes=minutes)],
    )
    return [int(v) for v in rows["game_pk"]] if not rows.is_empty() else []
