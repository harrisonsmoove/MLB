"""Command line interface.

The entry point for the backfill that this repository was built to run
elsewhere. The environment this code was written in had no network egress to
any upstream (the gateway refused CONNECT to every data host), so the parsers
and point-in-time guarantees are proven here against recorded fixtures and the
real multi-season load is a command you run where the network works:

    mlb-edge init
    mlb-edge backfill --start 2021-03-01 --end 2025-11-01
    mlb-edge verify

``verify`` runs the same integrity checks the test suite runs, against real
data. That is the half of Milestone 1 this environment could not prove.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from mlb_edge.config import Settings, load_settings
from mlb_edge.storage.rawcache import RawCache
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import utcnow

app = typer.Typer(
    add_completion=False,
    help="MLB simulation and betting system: ingestion, warehouse, integrity.",
    no_args_is_help=True,
)
odds_app = typer.Typer(help="Odds polling and closing-line capture.", no_args_is_help=True)
app.add_typer(odds_app, name="odds")

console = Console()


def _context(root: Path | None = None) -> tuple[Settings, Warehouse, RawCache]:
    settings = load_settings(root)
    warehouse = Warehouse.open(settings.warehouse_path)
    cache = RawCache(settings.raw_dir)
    return settings, warehouse, cache


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


# ---------------------------------------------------------------------------
# The ingest DAG. Order matters: game feeds need the schedule to know which
# game_pks exist, and weather needs venues to know where the parks are.
# ---------------------------------------------------------------------------
def _ingester_registry() -> dict[str, Any]:
    from mlb_edge.ingest.fangraphs import FangraphsProjectionsIngester
    from mlb_edge.ingest.kalshi import KalshiIngester
    from mlb_edge.ingest.mlb_statsapi import (
        MlbGameFeedIngester,
        MlbScheduleIngester,
        MlbTeamsIngester,
        MlbTransactionsIngester,
        MlbVenuesIngester,
    )
    from mlb_edge.ingest.odds import TheOddsApiIngester
    from mlb_edge.ingest.polymarket import PolymarketIngester
    from mlb_edge.ingest.retrosheet import RetrosheetIngester
    from mlb_edge.ingest.statcast import StatcastIngester
    from mlb_edge.ingest.weather import WeatherIngester

    return {
        "teams": MlbTeamsIngester,
        "venues": MlbVenuesIngester,
        "schedule": MlbScheduleIngester,
        "game_feeds": MlbGameFeedIngester,
        "transactions": MlbTransactionsIngester,
        "statcast": StatcastIngester,
        "retrosheet": RetrosheetIngester,
        "weather": WeatherIngester,
        "projections": FangraphsProjectionsIngester,
        "odds": TheOddsApiIngester,
        "kalshi": KalshiIngester,
        "polymarket": PolymarketIngester,
    }


BACKFILL_ORDER = (
    "teams",
    "venues",
    "schedule",
    "game_feeds",
    "transactions",
    "statcast",
    "retrosheet",
    "weather",
)


@app.command()
def init(
    root: Annotated[Path | None, typer.Option(help="Repo root (defaults to auto-detect).")] = None,
) -> None:
    """Create the warehouse and its directories."""
    settings, warehouse, cache = _context(root)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    from mlb_edge.storage import schema

    console.print(f"[green]warehouse[/green] {settings.warehouse_path}")
    console.print(f"[green]raw cache[/green] {settings.raw_dir}")
    console.print(f"[green]tables[/green]   {len(schema.TABLES)}")
    console.print(f"[green]sources[/green]  enabled: {', '.join(settings.enabled_sources())}")
    warehouse.close()


@app.command()
def backfill(
    start: Annotated[str, typer.Option(help="Inclusive start date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Inclusive end date, YYYY-MM-DD.")],
    sources: Annotated[
        str | None, typer.Option(help="Comma-separated subset of the DAG. Default: all.")
    ] = None,
    force_refresh: Annotated[bool, typer.Option(help="Ignore cache TTLs and re-fetch.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Plan only; make no requests.")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run the ingest DAG over a date range.

    Idempotent: re-running fetches only what cache TTLs say is stale, and rows
    already present at the same as-of timestamp are not duplicated. Safe to
    resume after an interruption, which matters for a multi-season pull.
    """
    settings, warehouse, cache = _context(root)
    registry = _ingester_registry()
    selected = (
        [s.strip() for s in sources.split(",")] if sources else list(BACKFILL_ORDER)
    )
    start_date, end_date = _parse_date(start), _parse_date(end)

    for name in selected:
        if name not in registry:
            console.print(f"[red]unknown source '{name}'[/red]; known: {sorted(registry)}")
            raise typer.Exit(code=2)
        source_key = {
            "teams": "mlb_statsapi",
            "venues": "mlb_statsapi",
            "schedule": "mlb_statsapi",
            "game_feeds": "mlb_statsapi",
            "transactions": "mlb_statsapi",
            "projections": "fangraphs",
        }.get(name, name)
        if not settings.source(source_key).enabled:
            console.print(f"[yellow]skip[/yellow] {name}: source '{source_key}' is disabled")
            continue

        ingester = registry[name](settings, cache=cache, warehouse=warehouse)
        console.print(f"[bold]{name}[/bold] {start_date} .. {end_date}")
        try:
            with warehouse.ingest_run(source_key, name, range_start=start_date, range_end=end_date) as state:
                kwargs: dict[str, Any] = {"force_refresh": force_refresh, "dry_run": dry_run}
                if name == "teams":
                    kwargs["seasons"] = settings.seasons
                if name == "retrosheet":
                    kwargs["seasons"] = None
                report = ingester.run(start_date, end_date, **kwargs)
                state["rows_written"] = report.total_rows
                state["versions_written"] = report.versions_written
            console.print(f"  {report.summary()}")
            for failure in report.failures[:5]:
                console.print(f"  [yellow]![/yellow] {failure}")
            if len(report.failures) > 5:
                console.print(f"  [yellow]![/yellow] ... and {len(report.failures) - 5} more")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]failed[/red] {type(exc).__name__}: {exc}")
        finally:
            ingester.close()

    warehouse.close()


@app.command(name="reload-cache")
def reload_cache(
    source: Annotated[str, typer.Argument(help="Ingester name from the DAG.")],
    as_of: Annotated[
        str | None,
        typer.Option(help="Rebuild as the warehouse would have looked on this UTC timestamp."),
    ] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Rebuild warehouse rows from cached payloads, with no network access.

    The reproducibility guarantee. Passing ``--as-of`` reconstructs the
    warehouse as it stood at that moment, ignoring every later upstream
    revision -- which is how you check whether a historical result moved because
    the model changed or because Statcast restated the data underneath it.
    """
    from mlb_edge.timeutil import parse_iso_utc

    settings, warehouse, cache = _context(root)
    registry = _ingester_registry()
    if source not in registry:
        console.print(f"[red]unknown source '{source}'[/red]")
        raise typer.Exit(code=2)

    ingester = registry[source](settings, cache=cache, warehouse=warehouse)
    cutoff = parse_iso_utc(as_of) if as_of else None
    report = ingester.reload_from_cache(as_of=cutoff)
    console.print(report.summary())
    for failure in report.failures[:10]:
        console.print(f"  [yellow]![/yellow] {failure}")
    ingester.close()
    warehouse.close()


@app.command()
def verify(
    root: Annotated[Path | None, typer.Option()] = None,
    strict: Annotated[bool, typer.Option(help="Exit non-zero on WARN as well as ERROR.")] = False,
) -> None:
    """Run every integrity check against the real warehouse.

    This is the Milestone 1 acceptance gate. The same checks run in CI against
    fixtures; running them here proves they hold on the actual multi-season
    load, which is the part a fixture cannot establish.
    """
    from mlb_edge import integrity

    _, warehouse, _ = _context(root)
    results = integrity.run_all(warehouse)

    table = Table(title="warehouse integrity", show_lines=False)
    table.add_column("status")
    table.add_column("check")
    table.add_column("detail", overflow="fold")
    for result in results:
        style = (
            "green"
            if result.passed
            else ("red" if result.severity == integrity.Severity.ERROR else "yellow")
        )
        status = "PASS" if result.passed else result.severity.value
        table.add_row(f"[{style}]{status}[/{style}]", result.name, result.detail)
    console.print(table)

    errors = [r for r in results if not r.passed and r.severity == integrity.Severity.ERROR]
    warnings = [r for r in results if not r.passed and r.severity == integrity.Severity.WARN]
    console.print(
        f"\n{len(results)} checks: [red]{len(errors)} errors[/red], "
        f"[yellow]{len(warnings)} warnings[/yellow]"
    )
    warehouse.close()
    if errors or (strict and warnings):
        raise typer.Exit(code=1)


@app.command()
def status(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Row counts and as-of coverage per table."""
    from mlb_edge.storage import schema

    settings, warehouse, cache = _context(root)
    table = Table(title=f"warehouse: {settings.warehouse_path}")
    table.add_column("table")
    table.add_column("kind")
    table.add_column("rows", justify="right")
    table.add_column("latest as_of")

    for spec in schema.TABLES:
        rows = warehouse.count(spec.name)
        latest = warehouse.max_as_of(spec.name) if spec.as_of_column else None
        table.add_row(
            spec.name,
            spec.kind.value,
            f"{rows:,}",
            latest.isoformat(timespec="seconds") if latest else "-",
        )
    console.print(table)

    entries = cache.iter_all()
    console.print(
        f"raw cache: {len(entries):,} payload versions, "
        f"{sum(e.n_bytes for e in entries) / 1e6:,.1f} MB"
    )
    warehouse.close()


@app.command()
def probe(
    source: Annotated[str, typer.Argument(help="Source name, e.g. fangraphs.")],
    dataset: Annotated[str | None, typer.Option(help="Restrict to one dataset.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Dump the observed keys of a cached payload.

    For the sources whose response shapes could not be verified offline
    (FanGraphs, Kalshi, Polymarket), this is how you correct the field maps in
    ``settings.yaml`` after the first live fetch: fetch once, probe, edit YAML,
    then ``reload-cache`` to reparse without re-fetching.
    """
    _, warehouse, cache = _context(root)
    entries = [
        e for e in cache.iter_all() if e.source == source and (not dataset or e.dataset == dataset)
    ]
    if not entries:
        console.print(f"[yellow]no cached payloads for source '{source}'[/yellow]")
        warehouse.close()
        raise typer.Exit(code=1)

    entry = entries[-1]
    console.print(f"[bold]{entry.dataset}/{entry.partition}[/bold] retrieved {entry.retrieved_at}")
    try:
        payload = json.loads(entry.read_text(cache.root))
    except json.JSONDecodeError:
        text = entry.read_text(cache.root)
        console.print("non-JSON payload; first line:")
        console.print(text.splitlines()[0][:500] if text else "(empty)")
        warehouse.close()
        return

    sample = payload
    while isinstance(sample, list) and sample:
        sample = sample[0]
    if isinstance(sample, dict):
        for key, value in sorted(sample.items())[:80]:
            preview = str(value)[:60].replace("\n", " ")
            console.print(f"  {key:32s} {type(value).__name__:8s} {preview}")
    else:
        console.print(f"  payload root is {type(payload).__name__}")
    warehouse.close()


@app.command(name="build-umpire-ratings")
def build_umpire_ratings(
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    cadence_days: Annotated[int, typer.Option(help="Snapshot cadence in days.")] = 7,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Derive expanding-window umpire ratings from Statcast."""
    from mlb_edge.ingest.umpires import UmpireRatingBuilder

    settings, warehouse, _ = _context(root)
    builder = UmpireRatingBuilder(warehouse, settings)
    written = builder.build(_parse_date(start), _parse_date(end), cadence_days=cadence_days)
    console.print(f"umpire_ratings: {written:,} rows written")
    warehouse.close()


@odds_app.command("poll")
def odds_poll(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """One odds snapshot into ``odds_snapshots``."""
    from mlb_edge.ingest.odds import TheOddsApiIngester

    settings, warehouse, cache = _context(root)
    ingester = TheOddsApiIngester(settings, cache=cache, warehouse=warehouse)
    today = utcnow().date()
    report = ingester.run(today, today)
    console.print(report.summary())
    if ingester.unresolved_events:
        console.print(f"[yellow]{len(ingester.unresolved_events)} events unresolved[/yellow]")
        for line in ingester.unresolved_events[:5]:
            console.print(f"  ! {line}")
    console.print(f"budget used this month: {ingester.requests_this_month()}")
    ingester.close()
    warehouse.close()


@odds_app.command("close")
def odds_close(
    minutes: Annotated[float, typer.Option(help="Capture games starting within N minutes.")] = 5.0,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Capture closing lines into ``closing_lines`` for CLV evaluation only."""
    from mlb_edge.ingest.odds import TheOddsApiIngester, games_closing_within

    settings, warehouse, cache = _context(root)
    ingester = TheOddsApiIngester(settings, cache=cache, warehouse=warehouse)
    upcoming = games_closing_within(warehouse, minutes)
    console.print(f"{len(upcoming)} games start within {minutes} minutes")
    report = ingester.capture_closing(minutes_before=minutes)
    console.print(report.summary())
    ingester.close()
    warehouse.close()


@app.command(name="budget")
def budget(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Odds API request budget consumed this calendar month."""
    from mlb_edge.ingest.odds import TheOddsApiIngester

    settings, warehouse, cache = _context(root)
    ingester = TheOddsApiIngester(settings, cache=cache, warehouse=warehouse)
    used = ingester.requests_this_month()
    allowance = ingester.config.get("monthly_request_budget")
    console.print(f"odds requests this month: {used} / {allowance or 'unlimited'}")
    ingester.close()
    warehouse.close()


if __name__ == "__main__":
    app()
