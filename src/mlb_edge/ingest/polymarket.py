"""Polymarket ingestion.

Same shape as Kalshi -- a CLOB with visible depth, where we can rest passive
orders -- but a different fee and settlement profile, and prices quoted in
dollars (0-1) rather than cents. Both land in the same ``market_quotes`` schema
so that nothing downstream has to know which venue a quote came from.

Market discovery goes through the Gamma API (events and their markets), and
depth through the CLOB ``/book`` endpoint keyed by token id. As with Kalshi, the
question text is matched against config-driven patterns and an unmatched market
is counted rather than guessed into a game.

Response shapes here could not be verified from this environment (no egress).
The parsers are defensive and tolerate missing keys by producing nulls; run
``mlb-edge probe polymarket`` after the first live fetch to check the field map.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.ingest.matching import GameMatcher
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import parse_iso_utc, utcnow


class PolymarketIngester(Ingester):
    source_name = "polymarket"
    writes_tables = ("market_quotes",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._matcher: GameMatcher | None = None
        self.unmapped_markets: list[str] = []
        self._token_map: dict[str, dict[str, Any]] = {}

    @property
    def matcher(self) -> GameMatcher:
        if self._matcher is None:
            if self.warehouse is None:
                raise RuntimeError("polymarket ingestion needs a warehouse to resolve game_pk")
            self._matcher = GameMatcher(self.warehouse, self.settings)
        return self._matcher

    def fee_schedule(self) -> dict[str, Any]:
        """Polymarket's fee and gas profile, fetched rather than assumed.

        Kept as a named method mirroring Kalshi's so the execution layer has one
        interface. Like Kalshi's, it refuses to invent numbers: an unavailable
        schedule halts the trading path.
        """
        from mlb_edge.ingest.kalshi import FeeScheduleUnavailable

        raise FeeScheduleUnavailable(
            "Polymarket fee/gas schedule fetching is not implemented. Trading on "
            "this venue stays disabled until it is: net-of-fee EV on a CLOB is "
            "the whole calculation, and a guessed gas cost is not a substitute."
        )

    # -- planning ------------------------------------------------------------
    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        """Offset-paginated event pages.

        Gamma pages with limit/offset. Requesting one page and stopping takes
        whatever the first page holds and silently drops the rest -- the same
        shape of bug as an unfollowed cursor. Pages are planned eagerly up to a
        bound; a page past the end returns an empty list and parses to nothing,
        which costs one cheap request rather than a truncated board.
        """
        label = kwargs.get("label") or utcnow().strftime("%Y%m%dT%H%M%SZ")
        gamma = str(self.config.get("gamma_url", "")).rstrip("/")
        path = (self.config.get("endpoints") or {})["gamma_events"]
        page_size = int(self.config.get("events_page_size", 200))
        max_pages = int(self.config.get("max_pages", 10))

        return [
            FetchTask(
                dataset="events",
                partition=f"{label}_p{page}",
                url=f"{gamma}{path}",
                params={
                    "closed": "false",
                    "limit": page_size,
                    "offset": page * page_size,
                    "tag_slug": "mlb",
                },
                max_age_seconds=0.0,
                context={"label": label, "page": page},
            )
            for page in range(max_pages)
        ]

    def plan_books(self, token_ids: list[str], *, label: str | None = None) -> list[FetchTask]:
        label = label or utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = (self.config.get("endpoints") or {})["book"]
        return [
            FetchTask(
                dataset="book",
                partition=f"{token_id}_{label}",
                url=f"{self.config.base_url}{path}",
                params={"token_id": token_id},
                max_age_seconds=0.0,
                context={"token_id": token_id},
            )
            for token_id in token_ids
        ]

    # -- parsing -------------------------------------------------------------
    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        data = json.loads(payload)
        if entry.dataset == "events":
            return {"market_quotes": self._parse_events(data, entry)}
        if entry.dataset == "book":
            return {"market_quotes": self._parse_book(data, entry, task)}
        return {}

    def _parse_events(self, data: Any, entry: RawEntry) -> pl.DataFrame:
        events = data if isinstance(data, list) else (data or {}).get("data", [])
        if not isinstance(events, list):
            return pl.DataFrame()

        prov = provenance_columns(entry, self.source_name)
        rows: list[dict[str, Any]] = []
        for event in events:
            for market in (event or {}).get("markets", []) or []:
                mapped = self._map_market(market, event)
                if mapped is None:
                    continue
                prices = _outcome_prices(market)
                for token_id in _token_ids(market):
                    self._token_map[token_id] = mapped
                rows.append(
                    {
                        "venue": "polymarket",
                        "game_pk": mapped["game_pk"],
                        "market_type": mapped["market_type"],
                        "line": mapped["line"],
                        "side": mapped["side"],
                        "quote_source": "summary",
                        "venue_ticker": str(market.get("conditionId") or market.get("id") or ""),
                        "best_bid": _to_float(market.get("bestBid")),
                        "best_ask": _to_float(market.get("bestAsk")),
                        "bid_size": None,
                        "ask_size": None,
                        "depth_bid_json": None,
                        "depth_ask_json": None,
                        "last_trade_price": prices[0] if prices else None,
                        "volume": _to_int(market.get("volume")),
                        "as_of_ts": entry.retrieved_ts,
                        **prov,
                    }
                )
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def _parse_book(self, data: Any, entry: RawEntry, task: FetchTask | None) -> pl.DataFrame:
        if not isinstance(data, dict):
            return pl.DataFrame()
        token_id = (
            str(task.context.get("token_id"))
            if task and task.context.get("token_id")
            else entry.partition.rsplit("_", 1)[0]
        )
        mapped = self._token_map.get(token_id)
        if mapped is None:
            return pl.DataFrame()

        bids = _book_side(data.get("bids"))
        asks = _book_side(data.get("asks"))
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)

        prov = provenance_columns(entry, self.source_name)
        return pl.DataFrame(
            [
                {
                    "venue": "polymarket",
                    "game_pk": mapped["game_pk"],
                    "market_type": mapped["market_type"],
                    "line": mapped["line"],
                    "side": mapped["side"],
                    "quote_source": "book",
                    "venue_ticker": token_id,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bid_size": sum(s for p, s in bids if p == best_bid) if best_bid else None,
                    "ask_size": sum(s for p, s in asks if p == best_ask) if best_ask else None,
                    "depth_bid_json": json.dumps(bids),
                    "depth_ask_json": json.dumps(asks),
                    "last_trade_price": None,
                    "volume": None,
                    "as_of_ts": entry.retrieved_ts,
                    **prov,
                }
            ]
        )

    def _map_market(self, market: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
        question = str(market.get("question") or event.get("title") or "")
        end_date = market.get("endDate") or event.get("endDate")
        if not question or not end_date:
            self.unmapped_markets.append(f"{market.get('id')}: missing question or endDate")
            return None

        for parser in self.settings.source("kalshi").get("market_parsers", []) or []:
            found = re.search(parser.get("title_pattern", ""), question)
            if not found:
                continue
            groups = found.groupdict()
            team_text = groups.get("team")
            if not team_text:
                continue
            game_pk, side = self._resolve_by_team(team_text, str(end_date))
            if game_pk is None:
                continue
            return {
                "game_pk": game_pk,
                "market_type": parser["market_type"],
                "side": side,
                "line": _to_float(groups.get("line")),
            }

        self.unmapped_markets.append(f"{market.get('id')}: no parser matched {question!r}")
        return None

    def _resolve_by_team(self, team_text: str, end_date: str) -> tuple[int | None, str]:
        if self.warehouse is None:
            return None, "home"
        team_id = self.matcher.team_id(team_text)
        if team_id is None:
            return None, "home"
        try:
            end_ts = parse_iso_utc(end_date)
        except ValueError:
            return None, "home"

        rows = self.warehouse.sql(
            """
            SELECT game_pk, home_team_id FROM (
                SELECT game_pk, home_team_id, scheduled_start_ts,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
                WHERE (home_team_id = ? OR away_team_id = ?)
                  AND scheduled_start_ts BETWEEN ? - INTERVAL 18 HOUR AND ?
            ) WHERE rn = 1
            ORDER BY scheduled_start_ts DESC
            """,
            [team_id, team_id, end_ts, end_ts],
        )
        if rows.height != 1:
            return None, "home"
        row = rows.row(0, named=True)
        return int(row["game_pk"]), "home" if int(row["home_team_id"]) == team_id else "away"


def _token_ids(market: dict[str, Any]) -> list[str]:
    """``clobTokenIds`` arrives as a JSON-encoded string in some responses."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(token) for token in raw] if isinstance(raw, list) else []


def _outcome_prices(market: dict[str, Any]) -> list[float]:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [p for p in (_to_float(v) for v in raw) if p is not None]


def _book_side(raw: Any) -> list[list[float]]:
    if not isinstance(raw, list):
        return []
    levels: list[list[float]] = []
    for item in raw:
        if isinstance(item, dict):
            price, size = _to_float(item.get("price")), _to_float(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = _to_float(item[0]), _to_float(item[1])
        else:
            continue
        if price is not None and size is not None:
            levels.append([price, size])
    return levels


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
