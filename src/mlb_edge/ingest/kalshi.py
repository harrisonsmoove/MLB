"""Kalshi ingestion.

Kalshi is not a sportsbook. It is a limit order book, and that difference drives
everything about how the system interacts with it:

* Real bid/ask depth is visible, so fair value is a depth-weighted mid rather
  than a devigged midpoint. A one-lot bid at 55 against 500 offered at 58 does
  not mean 56.5, and storing sizes is what makes that distinguishable later.
* Passive limit orders can rest inside the spread. That turns the model from a
  price-taker into a price-maker, which is a different and better business than
  crossing a sportsbook's vig.
* Fees are charged per contract and scale with price. They are **fetched**, never
  hardcoded: ``allow_hardcoded_fallback`` is false in config, so a failed fee
  fetch halts the trading path rather than letting a remembered formula quietly
  decide what counts as +EV.

Ticker and title formats could not be verified from this environment (no
egress). Market interpretation is therefore driven by the regex table in
``settings.yaml``; a market matching none of the patterns is counted as unmapped
and skipped rather than guessed into a game.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import date
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.ingest.matching import GameMatcher
from mlb_edge.market.prices import cents_to_prob
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import parse_iso_utc, utcnow


class FeeScheduleUnavailable(RuntimeError):
    """Raised when the live fee schedule cannot be fetched.

    Deliberately fatal for the trading path. Fee-adjusted EV is the only EV that
    matters on an exchange, and a stale or invented fee curve turns a losing
    trade into a winning-looking one.
    """


class KalshiAuth:
    """RSA-PSS request signing.

    Kalshi signs ``timestamp_ms + METHOD + path`` with the account's private key.
    Held here rather than in the HTTP layer so that an unauthenticated market
    data pull still works when no key is configured.
    """

    def __init__(self, api_key_id: str, private_key_path: str) -> None:
        from cryptography.hazmat.primitives import serialization

        self.api_key_id = api_key_id
        with open(private_key_path, "rb") as handle:
            self._key = serialization.load_pem_private_key(handle.read(), password=None)

    def headers(self, method: str, path: str) -> dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }


class KalshiIngester(Ingester):
    source_name = "kalshi"
    writes_tables = ("market_quotes",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._matcher: GameMatcher | None = None
        self._auth: KalshiAuth | None = None
        self.unmapped_markets: list[str] = []

    @property
    def matcher(self) -> GameMatcher:
        if self._matcher is None:
            if self.warehouse is None:
                raise RuntimeError("kalshi ingestion needs a warehouse to resolve game_pk")
            self._matcher = GameMatcher(self.warehouse, self.settings)
        return self._matcher

    @property
    def auth(self) -> KalshiAuth | None:
        if self._auth is None and self.config.has_secret("api_key_id"):
            self._auth = KalshiAuth(
                str(self.config.require("api_key_id")),
                str(self.config.require("private_key_path")),
            )
        return self._auth

    def _signed_headers(self, path: str, method: str = "GET") -> dict[str, str]:
        auth = self.auth
        return auth.headers(method, path) if auth else {}

    # -- fees ----------------------------------------------------------------
    def fee_schedule(self) -> dict[str, Any]:
        """Fetch the current fee schedule. Never falls back to a constant."""
        fees_cfg = self.config.get("fees", {}) or {}
        ttl_hours = float(fees_cfg.get("cache_ttl_hours", 24))
        cached = self.cache.latest(self.source_name, "fee_schedule", "current")
        if cached is not None and self.cache.has_fresh(
            self.source_name, "fee_schedule", "current", max_age_seconds=ttl_hours * 3600
        ):
            return json.loads(cached.read_text(self.cache.root))

        path = (self.config.get("endpoints") or {}).get("fee_schedule")
        if not path:
            raise FeeScheduleUnavailable("no fee_schedule endpoint configured for kalshi")
        try:
            response = self.client.get(
                f"{self.config.base_url}{path}", headers=self._signed_headers(path)
            )
        except Exception as exc:  # noqa: BLE001
            if fees_cfg.get("allow_hardcoded_fallback"):
                raise FeeScheduleUnavailable(
                    "fee fetch failed and a hardcoded fallback is not implemented on purpose"
                ) from exc
            raise FeeScheduleUnavailable(
                f"could not fetch the Kalshi fee schedule ({exc}). Trading is halted: "
                "fee-adjusted EV is the only EV that counts on an exchange, and this "
                "system will not substitute a remembered formula for the real one."
            ) from exc

        self.cache.store(
            source=self.source_name,
            dataset="fee_schedule",
            partition="current",
            payload=response.content,
            content_type="application/json",
            request_url=response.url,
            upstream_status=response.status,
        )
        return response.json()

    # -- planning ------------------------------------------------------------
    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        """List markets per series. Orderbooks are a second pass.

        Order books are fetched per ticker, and the tickers only exist once the
        market list has been parsed, so ``plan_orderbooks`` runs afterwards.
        """
        label = kwargs.get("label") or utcnow().strftime("%Y%m%dT%H%M%SZ")
        tasks: list[FetchTask] = []
        for series in self.config.get("series_tickers", []) or []:
            path = (self.config.get("endpoints") or {})["markets"]
            tasks.append(
                FetchTask(
                    dataset="markets",
                    partition=f"{series}_{label}",
                    url=f"{self.config.base_url}{path}",
                    params={"series_ticker": series, "status": "open", "limit": 200},
                    headers=self._signed_headers(path),
                    max_age_seconds=0.0,
                    context={"series": series, "label": label},
                )
            )
        return tasks

    def plan_orderbooks(self, tickers: list[str], *, label: str | None = None) -> list[FetchTask]:
        label = label or utcnow().strftime("%Y%m%dT%H%M%SZ")
        depth = int(self.config.get("orderbook_depth", 10))
        tasks: list[FetchTask] = []
        for ticker in tickers:
            path = (self.config.get("endpoints") or {})["orderbook"].format(ticker=ticker)
            tasks.append(
                FetchTask(
                    dataset="orderbook",
                    partition=f"{ticker}_{label}",
                    url=f"{self.config.base_url}{path}",
                    params={"depth": depth},
                    headers=self._signed_headers(path),
                    max_age_seconds=0.0,
                    context={"ticker": ticker},
                )
            )
        return tasks

    # -- parsing -------------------------------------------------------------
    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        data = json.loads(payload)
        if entry.dataset == "markets":
            return {"market_quotes": self._parse_markets(data, entry)}
        if entry.dataset == "orderbook":
            return {"market_quotes": self._parse_orderbook(data, entry, task)}
        return {}

    def _parse_markets(self, data: Any, entry: RawEntry) -> pl.DataFrame:
        markets = data.get("markets") if isinstance(data, dict) else data
        if not isinstance(markets, list):
            return pl.DataFrame()

        prov = provenance_columns(entry, self.source_name)
        rows: list[dict[str, Any]] = []
        for market in markets:
            mapped = self._map_market(market)
            if mapped is None:
                continue
            rows.append(
                {
                    "venue": "kalshi",
                    "game_pk": mapped["game_pk"],
                    "market_type": mapped["market_type"],
                    "line": mapped["line"],
                    "side": mapped["side"],
                    "venue_ticker": market.get("ticker"),
                    # Kalshi quotes whole cents on a $1 contract, so a price is
                    # already a probability -- no devigging, just a spread.
                    "best_bid": _prob(market.get("yes_bid")),
                    "best_ask": _prob(market.get("yes_ask")),
                    "bid_size": None,
                    "ask_size": None,
                    "depth_bid_json": None,
                    "depth_ask_json": None,
                    "last_trade_price": _prob(market.get("last_price")),
                    "volume": _to_int(market.get("volume")),
                    "as_of_ts": entry.retrieved_ts,
                    **prov,
                }
            )
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def _parse_orderbook(
        self, data: Any, entry: RawEntry, task: FetchTask | None
    ) -> pl.DataFrame:
        book = data.get("orderbook") if isinstance(data, dict) else None
        if not isinstance(book, dict):
            return pl.DataFrame()

        ticker = (
            str(task.context.get("ticker"))
            if task and task.context.get("ticker")
            else entry.partition.rsplit("_", 1)[0]
        )
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            return pl.DataFrame()

        yes_levels = _levels(book.get("yes"))
        no_levels = _levels(book.get("no"))
        # A resting NO bid at price p is an offer to sell YES at (100 - p), so
        # the YES ask side comes from the NO book. Reading only the YES side
        # would show a one-sided market that is in fact two-sided.
        best_bid = max((p for p, _ in yes_levels), default=None)
        best_ask = (
            100 - max((p for p, _ in no_levels), default=None) if no_levels else None
        )
        bid_size = sum(s for p, s in yes_levels if p == best_bid) if best_bid is not None else None
        ask_size = (
            sum(s for p, s in no_levels if 100 - p == best_ask) if best_ask is not None else None
        )

        prov = provenance_columns(entry, self.source_name)
        return pl.DataFrame(
            [
                {
                    "venue": "kalshi",
                    "game_pk": resolved["game_pk"],
                    "market_type": resolved["market_type"],
                    "line": resolved["line"],
                    "side": resolved["side"],
                    "venue_ticker": ticker,
                    "best_bid": _prob(best_bid),
                    "best_ask": _prob(best_ask),
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "depth_bid_json": json.dumps(yes_levels),
                    "depth_ask_json": json.dumps(no_levels),
                    "last_trade_price": None,
                    "volume": None,
                    "as_of_ts": entry.retrieved_ts,
                    **prov,
                }
            ]
        )

    # -- market interpretation ----------------------------------------------
    def _map_market(self, market: dict[str, Any]) -> dict[str, Any] | None:
        """Interpret a Kalshi market as (game_pk, market_type, side, line)."""
        title = " ".join(
            str(market.get(key) or "") for key in ("title", "subtitle", "yes_sub_title")
        ).strip()
        close_time = market.get("close_time") or market.get("expected_expiration_time")
        if not title or not close_time:
            self.unmapped_markets.append(f"{market.get('ticker')}: missing title or close_time")
            return None

        for parser in self.config.get("market_parsers", []) or []:
            pattern = parser.get("title_pattern")
            if not pattern:
                continue
            found = re.search(pattern, title)
            if not found:
                continue
            groups = found.groupdict()
            team_text = groups.get("team")
            line = _to_float(groups.get("line"))

            game_pk = None
            side = parser.get("side", "home")
            if team_text:
                game_pk, side = self._resolve_by_team(team_text, close_time)
            if game_pk is None:
                continue
            result = {
                "game_pk": game_pk,
                "market_type": parser["market_type"],
                "side": side,
                "line": line,
            }
            self._remember_ticker(str(market.get("ticker")), result)
            return result

        self.unmapped_markets.append(f"{market.get('ticker')}: no parser matched {title!r}")
        return None

    def _resolve_by_team(self, team_text: str, close_time: str) -> tuple[int | None, str]:
        """Find the game this team is playing near ``close_time``, and which side it is.

        Kalshi's close time is after the game ends, not at first pitch, so the
        window is wide and asymmetric. Matching still refuses on an ambiguous
        doubleheader rather than picking one.
        """
        if self.warehouse is None:
            return None, "home"
        team_id = self.matcher.team_id(team_text)
        if team_id is None:
            return None, "home"

        close_ts = parse_iso_utc(close_time)
        rows = self.warehouse.sql(
            """
            SELECT game_pk, home_team_id, scheduled_start_ts FROM (
                SELECT game_pk, home_team_id, scheduled_start_ts,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
                WHERE (home_team_id = ? OR away_team_id = ?)
                  AND scheduled_start_ts BETWEEN ? - INTERVAL 12 HOUR AND ?
            ) WHERE rn = 1
            ORDER BY scheduled_start_ts DESC
            """,
            [team_id, team_id, close_ts, close_ts],
        )
        if rows.height != 1:
            return None, "home"
        row = rows.row(0, named=True)
        side = "home" if int(row["home_team_id"]) == team_id else "away"
        return int(row["game_pk"]), side

    def _remember_ticker(self, ticker: str, mapping: dict[str, Any]) -> None:
        self._ticker_map = getattr(self, "_ticker_map", {})
        self._ticker_map[ticker] = mapping

    def _resolve_ticker(self, ticker: str) -> dict[str, Any] | None:
        """Look up a ticker mapped during the markets pass, else from the warehouse."""
        cached = getattr(self, "_ticker_map", {}).get(ticker)
        if cached is not None:
            return cached
        if self.warehouse is None:
            return None
        rows = self.warehouse.sql(
            "SELECT game_pk, market_type, line, side FROM market_quotes "
            "WHERE venue = 'kalshi' AND venue_ticker = ? ORDER BY as_of_ts DESC LIMIT 1",
            [ticker],
        )
        return rows.row(0, named=True) if not rows.is_empty() else None


def _levels(raw: Any) -> list[list[int]]:
    """Normalise ``[[price, size], ...]``, dropping malformed entries."""
    if not isinstance(raw, list):
        return []
    levels: list[list[int]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = _to_int(item[0]), _to_int(item[1])
            if price is not None and size is not None:
                levels.append([price, size])
    return levels


def _prob(cents: Any) -> float | None:
    value = _to_int(cents)
    return cents_to_prob(value) if value is not None else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
