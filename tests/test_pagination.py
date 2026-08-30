"""Every place that pages, chunks or splits, exercised across the boundary.

The Arrow-reader bug was not really a DuckDB bug. It was a testing bug: every
fixture fit in one pass, so a code path that truncated after the first batch
looked identical to one that worked. These tests exist so that no paging path
is only ever seen doing a single iteration.

The assertion in each case is a **total row count**, not "it returned without
raising". A truncated fetch succeeds; that is what makes it dangerous.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from mlb_edge.config import load_settings
from mlb_edge.http import Response

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class PagingStub:
    """Returns a fixed number of pages, then signals the end."""

    def __init__(self, pages: list[dict], cursor_key: str = "cursor") -> None:
        self.pages = pages
        self.cursor_key = cursor_key
        self.calls: list[dict] = []

    def get(self, url, *, params=None, headers=None):
        params = dict(params or {})
        self.calls.append(params)
        if "orderbook" in url:
            body = {"orderbook": {"yes": [[55, 10]], "no": [[44, 20]]}}
            return _response(url, body)

        cursor = params.get(self.cursor_key)
        index = int(cursor) if cursor else 0
        page = self.pages[index] if index < len(self.pages) else {"markets": []}
        return _response(url, page)

    def close(self):
        pass


def _response(url, body):
    return Response(
        url=url,
        status=200,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


def _market_pages(n_pages: int, per_page: int) -> list[dict]:
    """Pages of markets, each pointing at the next; the last has no cursor."""
    pages = []
    for page in range(n_pages):
        pages.append(
            {
                "markets": [
                    {
                        "ticker": f"KXMLBGAME-T{page * per_page + i}",
                        "title": "Will the New York Yankees win?",
                        "close_time": "2025-04-01T21:00:00Z",
                        "yes_bid": 50,
                        "yes_ask": 53,
                    }
                    for i in range(per_page)
                ],
                "cursor": str(page + 1) if page + 1 < n_pages else "",
            }
        )
    return pages


# ---------------------------------------------------------------------------
# Kalshi poller: cursor
# ---------------------------------------------------------------------------
def test_poller_follows_the_cursor_past_the_first_page(monkeypatch, request):
    """Three pages, not one. A capped single request drops most of the board."""
    from mlb_edge.poll import KalshiPoller

    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    settings = load_settings(request.config.rootpath)
    stub = PagingStub(_market_pages(n_pages=3, per_page=4))
    poller = KalshiPoller(settings, client=stub)
    poller.poll_config = dict(poller.poll_config) | {"kalshi_max_orderbooks_per_tick": 100}

    records = poller.poll()

    market_records = [r for r in records if r.endpoint == "markets"]
    books = [r for r in records if r.endpoint == "orderbook"]
    series_count = len(settings.source("kalshi").get("series_tickers"))

    assert len(market_records) == 3 * series_count, "one record per page per series"
    assert len(books) == 12 * series_count, "every ticker on every page gets a book"
    assert any("cursor" in call for call in stub.calls), "the cursor was never sent"


def test_poller_stops_when_the_cursor_clears(monkeypatch, request):
    from mlb_edge.poll import KalshiPoller

    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    settings = load_settings(request.config.rootpath)
    stub = PagingStub(_market_pages(n_pages=1, per_page=2))
    poller = KalshiPoller(settings, client=stub)

    records = poller.poll()
    market_calls = [c for c in stub.calls if "series_ticker" in c]
    series_count = len(settings.source("kalshi").get("series_tickers"))

    assert len(market_calls) == series_count, "no extra request after the last page"
    assert len([r for r in records if r.endpoint == "markets"]) == series_count


def test_poller_bounds_a_cursor_that_never_clears(monkeypatch, request):
    """A cursor loop must terminate, and say so rather than silently stopping."""
    from mlb_edge.poll import KalshiPoller

    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    settings = load_settings(request.config.rootpath)

    class NeverEnds(PagingStub):
        def get(self, url, *, params=None, headers=None):
            self.calls.append(dict(params or {}))
            return _response(url, {"markets": [{"ticker": "T"}], "cursor": "always"})

    stub = NeverEnds([])
    poller = KalshiPoller(settings, client=stub)
    poller.poll_config = dict(poller.poll_config) | {
        "kalshi_max_pages": 4,
        "kalshi_max_orderbooks_per_tick": 0,
    }
    records = poller.poll()

    series_count = len(settings.source("kalshi").get("series_tickers"))
    assert len(stub.calls) == 4 * series_count, "the loop must be bounded"
    truncation_warnings = [r for r in records if r.error and "truncated" in r.error]
    assert len(truncation_warnings) == series_count, (
        "hitting the page bound must be archived as a failure, not passed over"
    )


# ---------------------------------------------------------------------------
# Kalshi ingester: cursor
# ---------------------------------------------------------------------------
def test_ingester_loads_markets_from_every_page(monkeypatch, request, tmp_path):
    from mlb_edge.ingest.kalshi import KalshiIngester
    from mlb_edge.storage.rawcache import RawCache

    settings = load_settings(request.config.rootpath)
    warehouse = _warehouse_with_games(settings)
    stub = PagingStub(_market_pages(n_pages=3, per_page=5))

    ingester = KalshiIngester(
        settings, cache=RawCache(tmp_path), warehouse=warehouse, client=stub
    )
    report = ingester.run(date(2025, 4, 1), date(2025, 4, 1))

    series_count = len(settings.source("kalshi").get("series_tickers"))
    pages_fetched = warehouse.sql(
        "SELECT count(*) AS n FROM raw_manifest WHERE source = 'kalshi' AND dataset = 'markets'"
    )["n"][0]

    assert pages_fetched == 3 * series_count, (
        f"expected 3 pages per series to be fetched, got {pages_fetched}"
    )
    # Every ticker from every page reached the mapper. Asserting on
    # market_quotes rows would not show this: the synthetic tickers all share a
    # title, so they resolve to one logical market and correctly deduplicate.
    assert len(ingester._ticker_map) == 15, (
        f"tickers from later pages went missing: {len(ingester._ticker_map)}"
    )
    assert any("cursor" in call for call in stub.calls)
    assert not report.failures
    warehouse.close()


def _warehouse_with_games(settings):
    """A warehouse the Kalshi matcher can resolve tickers against."""
    import polars as pl

    from mlb_edge.storage.warehouse import Warehouse

    warehouse = Warehouse.in_memory()
    warehouse.load(
        "teams",
        pl.DataFrame(
            [
                {
                    "team_id": tid, "season": 2025, "name": name, "abbreviation": abbr,
                    "as_of_ts": NOW, "source": "test", "ingested_at": NOW,
                }
                for tid, name, abbr in ((147, "New York Yankees", "NYY"),
                                        (111, "Boston Red Sox", "BOS"))
            ]
        ),
    )
    warehouse.load(
        "games",
        pl.DataFrame(
            [
                {
                    "game_pk": 776001, "season": 2025, "game_type": "R",
                    "game_date_local": date(2025, 4, 1),
                    "scheduled_start_ts": datetime(2025, 4, 1, 17, 5, tzinfo=UTC),
                    "home_team_id": 147, "away_team_id": 111,
                    "as_of_ts": NOW, "source": "test", "ingested_at": NOW,
                }
            ]
        ),
    )
    return warehouse


# ---------------------------------------------------------------------------
# Polymarket: offset
# ---------------------------------------------------------------------------
def test_polymarket_plans_more_than_one_offset_page(request):
    from mlb_edge.ingest.polymarket import PolymarketIngester

    settings = load_settings(request.config.rootpath)
    tasks = PolymarketIngester(settings).plan(date(2025, 4, 1), date(2025, 4, 1))

    assert len(tasks) > 1, "a single page silently drops the rest of the board"
    offsets = [t.params["offset"] for t in tasks]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    assert offsets[0] == 0
    page_size = tasks[0].params["limit"]
    assert offsets[1] == page_size, "offsets must step by the page size"


# ---------------------------------------------------------------------------
# Statcast: the 30,000 row cap
# ---------------------------------------------------------------------------
class StatcastStub:
    """Returns rows proportional to the requested span, capped like Savant."""

    def __init__(self, rows_per_day: int, row_cap: int) -> None:
        self.rows_per_day = rows_per_day
        self.row_cap = row_cap
        self.calls: list[tuple[str, str]] = []

    def get(self, url, *, params=None, headers=None):
        start = date.fromisoformat(params["game_date_gt"])
        end = date.fromisoformat(params["game_date_lt"])
        self.calls.append((params["game_date_gt"], params["game_date_lt"]))
        days = (end - start).days + 1

        header = (
            "game_pk,game_date,at_bat_number,pitch_number,inning,inning_topbot,batter,"
            "pitcher,stand,p_throws,events,description,type,zone,balls,strikes,"
            "outs_when_up,on_1b,on_2b,on_3b,pitch_type,release_speed,plate_x,plate_z,"
            "sz_top,sz_bot,launch_speed,launch_angle,"
            "estimated_woba_using_speedangle,woba_value,woba_denom,delta_run_exp,"
            "bat_score,fld_score,bb_type"
        )
        lines = [header]
        for day in range(days):
            game_date = start.replace() if days == 1 else start
            from datetime import timedelta as _td

            game_date = start + _td(days=day)
            for i in range(self.rows_per_day):
                lines.append(
                    f"{900000 + day},{game_date.isoformat()},{i + 1},1,1,Top,1,2,R,R,"
                    f"strikeout,swinging_strike,S,5,0,2,0,,,,FF,95.0,0.1,2.3,3.4,1.6,"
                    ",,,0,1,-0.1,0,0,"
                )
        # Savant truncates silently at the cap.
        body = "\n".join(lines[: self.row_cap + 1]) + "\n"
        return Response(
            url=url, status=200, content=body.encode(), headers={"content-type": "text/csv"}
        )

    def close(self):
        pass


def test_statcast_splits_a_truncated_chunk_until_every_row_lands(request, tmp_path):
    """The adaptive split loop, never previously executed by a test.

    Only ``_is_truncated`` was unit-tested; the recursion that acts on it was
    not. A silently capped response is exactly the failure this code exists to
    prevent, so it needs to be seen happening.
    """
    from mlb_edge.ingest.statcast import StatcastIngester
    from mlb_edge.storage.rawcache import RawCache
    from mlb_edge.storage.warehouse import Warehouse

    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()

    rows_per_day, row_cap = 12, 20
    stub = StatcastStub(rows_per_day=rows_per_day, row_cap=row_cap)
    ingester = StatcastIngester(
        settings, cache=RawCache(tmp_path), warehouse=warehouse, client=stub
    )
    ingester.config.raw["row_cap"] = row_cap
    ingester.config.raw["chunk_days"] = 3

    report = ingester.run(date(2025, 4, 1), date(2025, 4, 3), today=date(2025, 5, 1))

    assert len(stub.calls) > 1, "a capped 3-day chunk must have been split"
    assert ("2025-04-01", "2025-04-01") in stub.calls, "split down to single days"
    assert warehouse.count("statcast_pitches") == rows_per_day * 3, (
        "every row across all three days must land, not just the first chunk's"
    )
    assert not report.failures
    warehouse.close()


def test_statcast_reports_a_single_day_over_the_cap_rather_than_truncating(
    request, tmp_path
):
    """One day above the cap cannot be split further, so it must be loud."""
    from mlb_edge.ingest.statcast import StatcastIngester
    from mlb_edge.storage.rawcache import RawCache
    from mlb_edge.storage.warehouse import Warehouse

    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()
    stub = StatcastStub(rows_per_day=50, row_cap=20)
    ingester = StatcastIngester(
        settings, cache=RawCache(tmp_path), warehouse=warehouse, client=stub
    )
    ingester.config.raw["row_cap"] = 20
    ingester.config.raw["chunk_days"] = 1

    report = ingester.run(date(2025, 4, 1), date(2025, 4, 1), today=date(2025, 5, 1))

    assert report.failures, "incomplete data must be reported, never accepted silently"
    assert "exceeds" in report.failures[0]
    warehouse.close()


@pytest.mark.parametrize("chunk_days,span", [(3, 7), (1, 4), (7, 14)])
def test_statcast_covers_the_whole_range_whatever_the_chunking(
    request, tmp_path, chunk_days, span
):
    """Chunk size is an implementation detail; coverage must not depend on it."""
    from datetime import timedelta

    from mlb_edge.ingest.statcast import StatcastIngester
    from mlb_edge.storage.rawcache import RawCache
    from mlb_edge.storage.warehouse import Warehouse

    settings = load_settings(request.config.rootpath)
    warehouse = Warehouse.in_memory()
    stub = StatcastStub(rows_per_day=8, row_cap=20)
    ingester = StatcastIngester(
        settings, cache=RawCache(tmp_path / f"c{chunk_days}"), warehouse=warehouse, client=stub
    )
    ingester.config.raw["row_cap"] = 20
    ingester.config.raw["chunk_days"] = chunk_days

    start = date(2025, 4, 1)
    ingester.run(start, start + timedelta(days=span - 1), today=date(2025, 6, 1))

    assert warehouse.count("statcast_pitches") == 8 * span
    warehouse.close()
