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
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from mlb_edge.config import ConfigError, Settings
from mlb_edge.config import load_settings as _load_settings_raw
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


#: Where deploy.sh writes the service environment file. Named in the error
#: below so a hand-run command says what to do rather than what went wrong.
ENV_FILE = Path("/etc/mlb-edge/mlb-edge.env")


def _credentials_hint() -> str:
    if ENV_FILE.is_file():
        return (
            f"\nThis command needs the service environment. Run:\n"
            f"  set -a; . {ENV_FILE}; set +a\n"
            "and try again. systemd loads it for the daemon; your shell does not."
        )
    return (
        f"\nExpected credentials in {ENV_FILE}, which does not exist.\n"
        "Copy deploy/mlb-edge.env.example to it, fill it in, then:\n"
        f"  set -a; . {ENV_FILE}; set +a"
    )


def load_settings(root: Path | None = None) -> Settings:
    """``config.load_settings`` with a human-readable failure.

    Validation failing hard on an unset secret is correct for the daemon -- fail
    at startup, not mid-backfill. As a stack trace to someone who has just
    ssh-ed in and run ``poll-status`` it is useless: the answer is always "you
    did not source the env file", and the traceback never says so.
    """
    try:
        return _load_settings_raw(root)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        console.print(_credentials_hint())
        raise typer.Exit(2) from None



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


@app.command(name="gate")
def gate_command(
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Check whether work may begin under model/.

    Two conditions, in this order: integrity clean, then the pre-registered
    strikeout constant inside its ratio band. The order is enforced -- a
    constant fitted on a warehouse that fails its own integrity checks is a
    number derived from corrupted input, and letting it pass would look like
    permission to proceed.

    Writes reports/gate.json. The test suite refuses any module under model/
    without a passing record, so this is a real gate rather than a reminder.
    """
    from mlb_edge import gate as gate_module

    settings, warehouse, _ = _context(root)
    result = gate_module.evaluate(warehouse)

    console.print("[bold]1. integrity[/bold]")
    if result.integrity_passed:
        console.print("  [green]PASS[/green] no ERROR-level checks failed")
    else:
        console.print(f"  [red]FAIL[/red] {len(result.integrity_errors)} errors")
        for line in result.integrity_errors[:10]:
            console.print(f"    {line}")

    console.print("\n[bold]2. pre-registered constant[/bold]")
    if not result.integrity_passed:
        console.print("  [dim]not evaluated -- integrity must pass first[/dim]")
    elif not result.constant_lines:
        console.print(f"  [yellow]not evaluated[/yellow] -- {result.blocked_reason}")
    else:
        for line in result.constant_lines:
            style = "green" if line.startswith("PASS") else "red"
            console.print(f"  [{style}]{line}[/{style}]")
        for note in result.diagnosis:
            console.print(f"  [dim]{note}[/dim]")

    # Whether the record being replaced still described this warehouse. A
    # verdict written before a rebuild is a statement about data that is gone.
    previous = gate_module.read_record(settings.reports_dir)
    if previous is not None:
        was_current, changes = gate_module.record_is_current(previous, warehouse)
        if was_current:
            console.print("\n[dim]previous record was still current[/dim]")
        else:
            console.print("\n[yellow]previous record was stale[/yellow]")
            for change in changes[:6]:
                console.print(f"  [dim]{change}[/dim]")

    path = gate_module.write_record(result, settings.reports_dir)
    tables = result.fingerprint.get("tables", {})
    populated = sum(1 for t in tables.values() if t.get("rows"))
    console.print(
        f"\n[dim]fingerprint {result.fingerprint.get('digest', '')[:16]} "
        f"over {populated} populated tables[/dim]"
    )
    verdict = "[green]GATE PASSED[/green]" if result.passed else "[red]GATE BLOCKED[/red]"
    console.print(f"\n{verdict}")
    if not result.passed:
        console.print(f"  {result.blocked_reason}")
    console.print(f"[dim]record: {path}[/dim]")

    warehouse.close()
    if not result.passed:
        raise typer.Exit(code=1)


backup_app = typer.Typer(help="Off-box backup and restore.", no_args_is_help=True)
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(
    push: Annotated[bool, typer.Option(help="Run the configured upload command after.")] = True,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Snapshot the archive and warehouse, verify it, and push it off-box."""
    from mlb_edge import backup as backup_module

    settings = load_settings(root)
    config = settings.section("backup")
    backup_root = settings.root / config.get("directory", "data/backups")
    destination = backup_root / utcnow().strftime("%Y-%m-%dT%H%M%SZ")

    manifest = backup_module.create(
        root=settings.root,
        destination=destination,
        warehouse_path=settings.warehouse_path,
        include=tuple(config.get("include", ["data/poll", "data/raw"])),
    )
    console.print(f"created {destination.name}: {manifest.summary()}")

    ok, problems = backup_module.verify(destination)
    if not ok:
        console.print(f"[red]verification failed[/red] ({len(problems)} problems)")
        for line in problems[:10]:
            console.print(f"  {line}")
        raise typer.Exit(code=1)
    console.print("[green]verified[/green] every checksum matches")

    command = str(config.get("push_command") or "").strip()
    if push and command:
        pushed, output = backup_module.push(backup_dir=destination, command=command)
        if pushed:
            console.print("[green]pushed off-box[/green]")
        else:
            # Loud: a local-only backup does not survive the failure it exists for.
            console.print(f"[red]push FAILED[/red] -- this backup is local only\n{output}")
            raise typer.Exit(code=1)
    elif push:
        console.print(
            "[yellow]no push_command configured[/yellow] -- backup is local only, "
            "which does not survive the droplet dying"
        )

    removed = backup_module.prune(backup_root, keep=int(config.get("keep_local", 3)))
    if removed:
        console.print(f"pruned {len(removed)} older local backups")


@backup_app.command("verify")
def backup_verify(
    backup_dir: Annotated[Path, typer.Argument(help="Backup directory to check.")],
) -> None:
    """Recompute every checksum in a backup."""
    from mlb_edge import backup as backup_module

    ok, problems = backup_module.verify(backup_dir)
    manifest = backup_module.read_manifest(backup_dir)
    console.print(f"{manifest.summary()} taken {manifest.created_at}")
    if ok:
        console.print("[green]OK[/green] every checksum matches")
        return
    console.print(f"[red]{len(problems)} problems[/red]")
    for line in problems[:20]:
        console.print(f"  {line}")
    raise typer.Exit(code=1)


@backup_app.command("restore")
def backup_restore(
    backup_dir: Annotated[Path, typer.Argument(help="Backup directory to restore from.")],
    into: Annotated[Path, typer.Option(help="Root to restore into.")],
    skip_verify: Annotated[bool, typer.Option(help="Restore without checking checksums.")] = False,
) -> None:
    """Restore a backup into a root directory.

    Restores into a directory you name rather than over the live one. Recovery
    is not the moment to discover the backup was corrupt after overwriting the
    only other copy -- check the result, then move it into place.
    """
    from mlb_edge import backup as backup_module

    warehouse_path = Path(into) / "data" / "warehouse" / "mlb_edge.duckdb"
    manifest, problems = backup_module.restore(
        backup_dir=backup_dir,
        into_root=into,
        warehouse_path=warehouse_path,
        verify_first=not skip_verify,
    )
    console.print(f"restored {manifest.summary()} from {manifest.created_at}")
    if problems:
        console.print(f"[red]{len(problems)} problems[/red]")
        for line in problems[:20]:
            console.print(f"  {line}")
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/green] restored into {into}")
    console.print(f"  warehouse: {warehouse_path}")
    console.print(f"  check it, then swap it in:  mv {into}/data <live>/data")


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


parks_app = typer.Typer(help="Park metadata and orientation.", no_args_is_help=True)
app.add_typer(parks_app, name="parks")


@parks_app.command("bearings")
def parks_bearings(
    season: Annotated[int, typer.Option(help="Season whose active parks to report.")] = 2026,
    missing_only: Annotated[bool, typer.Option(help="List only the unmeasured parks.")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Report cf_bearing_deg coverage across active parks.

    The wind feature stays disabled until these are measured -- a guessed
    bearing is worse than a missing one, because a 180-degree error is a
    plausible number that silently inverts every wind adjustment at that park.
    """
    from mlb_edge.features.park import active_parks, orientation_coverage

    settings = load_settings(root)
    have, missing = orientation_coverage(settings, season)
    total = len(active_parks(settings, season))

    table = Table(title=f"cf_bearing_deg coverage, {season} ({len(have)}/{total} measured)")
    table.add_column("park")
    table.add_column("bearing", justify="right")
    table.add_column("source")

    shown = missing if missing_only else sorted(have + missing, key=lambda p: p.slug)
    for park in shown:
        if park.has_orientation:
            table.add_row(park.slug, f"{park.cf_bearing_deg:.0f}deg", park.orientation_source)
        else:
            table.add_row(park.slug, "[yellow]unset[/yellow]", "-")
    console.print(table)

    if missing:
        console.print(
            f"\n[yellow]{len(missing)} of {total} still unmeasured.[/yellow] Wind components "
            "resolve to null at those parks, and features/environment raises rather than "
            "guessing."
        )
        console.print(
            "Measure home plate -> dead centre off true north, set orientation_source, "
            "then: uv run pytest tests/test_wind_sign_convention.py"
        )
        console.print(
            "[dim]Enter wrigley_field first: the suite checks it against a south-west "
            "wind blowing out, which is what catches a 180-degree flip.[/dim]"
        )
    else:
        console.print(f"\n[green]all {total} active parks measured[/green]")


@app.command(name="build-pa-outcomes")
def build_pa_outcomes(
    seasons: Annotated[str | None, typer.Option(help="Comma-separated seasons; default all.")] = None,
    chunk_rows: Annotated[int, typer.Option(help="Rows per streamed chunk.")] = 250_000,
    staging_dir: Annotated[Path | None, typer.Option(help="Where parquet chunks land.")] = None,
    keep_staging: Annotated[bool, typer.Option(help="Keep the parquet chunks after loading.")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Collapse pitch-level Statcast into plate appearances.

    Streams through parquet in fixed-size chunks, so peak memory is one chunk
    regardless of how many seasons are in scope -- running all eleven at once is
    no heavier than running one.

    Reports taxonomy coverage. An `events` value matching nothing in the
    configured taxonomy is counted and named rather than swept into OUT, so a
    vocabulary change upstream shows up here instead of quietly biasing one
    bucket.
    """
    from mlb_edge.features.pa_outcomes import PaOutcomeExtractor

    settings, warehouse, _ = _context(root)
    season_list = [int(s) for s in seasons.split(",")] if seasons else None
    staging = Path(staging_dir) if staging_dir else settings.root / "data" / "staging" / "pa"

    extractor = PaOutcomeExtractor(settings)
    report, rows = extractor.extract_to_warehouse(
        warehouse,
        staging_dir=staging,
        seasons=season_list,
        chunk_rows=chunk_rows,
        keep_staging=keep_staging,
        progress=lambda chunks, written: console.print(
            f"  chunk {chunks}: {written:,} plate appearances staged", highlight=False
        ),
    )
    if report.plate_appearances == 0:
        console.print("[yellow]no plate appearances extracted[/yellow] -- is statcast loaded?")
        warehouse.close()
        raise typer.Exit(code=1)

    console.print(report.summary())
    console.print(f"pa_outcomes: {rows:,} rows written")
    if report.unknown_events:
        console.print(
            f"[yellow]{len(report.unknown_events)} unrecognised event types[/yellow] -- "
            "add them to pa_outcomes.events in settings.yaml, then re-run"
        )
        for event, count in report.unknown_events.most_common(15):
            console.print(f"  {event}: {count:,}")
    warehouse.close()


@app.command(name="build-projections")
def build_projections(
    start: Annotated[str, typer.Option(help="First snapshot date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last snapshot date, YYYY-MM-DD.")],
    player_types: Annotated[str, typer.Option(help="batter, pitcher, or both.")] = "batter,pitcher",
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Snapshot in-house projections on the configured cadence.

    Each snapshot reads plate appearances strictly before its own date and
    refits the contact table on the same restricted history, so a projection
    dated 12 May knows nothing about 12 May. That is what makes these usable in
    a walk-forward backtest rather than only going forward.
    """
    from mlb_edge.features.battedball import fit_batted_ball_model, model_to_frame
    from mlb_edge.features.preregistration import (
        GATING_PLAYER_TYPE,
        gating_result,
        interpret,
    )
    from mlb_edge.features.ratings import Projector, constants_frame

    settings, warehouse, _ = _context(root)
    gating_report = None
    other_reports: list[tuple[str, Any]] = []
    projector = Projector(settings, warehouse)
    dates = projector.snapshot_dates(_parse_date(start), _parse_date(end))
    types = [t.strip() for t in player_types.split(",") if t.strip()]

    console.print(f"{len(dates)} snapshots x {len(types)} player types")
    total = 0
    for snapshot in dates:
        # One contact table per snapshot, shared by batters and pitchers: it is
        # a league-wide table, and refitting it twice would only cost time.
        model = fit_batted_ball_model(
            warehouse,
            through=snapshot,
            pooling_k=float(settings.section("projector").get("battedball_pooling_k", 40)),
        )
        lookup = model_to_frame(model)
        if not lookup.is_empty():
            warehouse.load("battedball_lookup", lookup)

        for player_type in types:
            rates, report = projector.build(snapshot, player_type=player_type, model=model)
            if rates.is_empty():
                console.print(f"  {snapshot} {player_type}: no data")
                continue
            written = warehouse.load("pa_rates", rates).rows_written
            total += written
            warehouse.load(
                "projector_constants",
                constants_frame(
                    report,
                    system=str(settings.section("projector").get("system_name")),
                    player_type=player_type,
                ),
            )
            console.print(f"  {player_type}: {report.summary()} -> {written:,} rows")
            # Gate on the batter fit only: the pre-registered target is derived
            # from hitter talent spread and does not transfer to pitchers.
            if player_type == GATING_PLAYER_TYPE:
                gating_report = report
            else:
                other_reports.append((player_type, report))

    console.print(f"\npa_rates: {total:,} rows written")

    # The pre-registered check, printed without being asked for. The target was
    # fixed before any real data was seen; see features/preregistration.py.
    if gating_report is not None and gating_report.constants:
        gate_min_trials = float(settings.section("projector").get("gate_min_trials", 300))
        console.print(
            f"\n[bold]pre-registered constant check[/bold] ({GATING_PLAYER_TYPE}, "
            f"gated at min_pa>={gate_min_trials:.0f})"
        )
        for line in gating_report.preregistration_lines(gate_min_trials):
            style = "green" if line.startswith("PASS") else "red"
            console.print(f"  [{style}]{line}[/{style}]")

        # Ratio against population. If it moves with the threshold, the miss is
        # sample composition; if it holds, the estimator is the suspect.
        console.print("\n  [bold]ratio vs population[/bold]")
        for line in gating_report.population_lines():
            console.print(f"    {line}")

        passed, reason = gating_result(gating_report.preregistration(gate_min_trials))
        if passed:
            console.print(f"\n  [green]gate: PASS[/green] ({reason})")
        else:
            console.print(f"\n  [red]gate: FAIL[/red] ({reason})")
            for check in gating_report.preregistration(gate_min_trials):
                if check.gating and not check.passed:
                    console.print(f"  {interpret(check)}")
            ratios = [
                gating_report.preregistration(t)[0].ratio
                for t in sorted(gating_report.constant_fits)
                if gating_report.preregistration(t)
            ]
            if len(ratios) > 1 and max(ratios) > 1.5 * min(ratios):
                console.print(
                    "  [yellow]the ratio moves substantially with the playing-time "
                    "threshold, so sample composition is a live explanation before "
                    "the estimator is.[/yellow]"
                )
    elif gating_report is None:
        console.print(
            f"\n[yellow]no {GATING_PLAYER_TYPE} fit ran, so the gate did not "
            "evaluate.[/yellow] It cannot pass by not running."
        )

    for player_type, report in other_reports:
        console.print(f"\n[dim]{player_type} constants (not gated):[/dim]")
        for line in report.preregistration_lines():
            console.print(f"  [dim]{line.replace('FAIL', 'n/a ').replace('PASS', 'n/a ')}[/dim]")
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


@app.command(name="poll-daemon")
def poll_daemon(
    once: Annotated[bool, typer.Option(help="Run a single tick per source and exit.")] = False,
    max_ticks: Annotated[int | None, typer.Option(help="Stop after N total ticks.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run the archive poller. This is the process systemd supervises.

    Writes raw payloads to timestamped parquet and nothing else -- no parsing,
    no warehouse, no DuckDB lock. The CLV dataset only accumulates in wall-clock
    time, so this process staying up matters more than anything downstream of it
    being correct.
    """
    from mlb_edge.poll import PollDaemon

    settings = load_settings(root)
    daemon = PollDaemon(settings)
    daemon.install_signal_handlers()
    daemon.run(once=once, max_ticks=max_ticks)


@app.command(name="refresh-schedule")
def refresh_schedule(
    days_ahead: Annotated[int, typer.Option(help="How far forward to pull the slate.")] = 7,
    days_back: Annotated[int, typer.Option(help="How far back to re-pull for status changes.")] = 2,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Refresh teams, venues and the schedule around today.

    Run daily. Not optional if the poll archive is to be usable: the archive
    stores a book's team names and commence time, and resolving those to a
    game_pk needs the schedule and teams dimension present. Looking backwards a
    couple of days catches postponements and suspended games that get a
    resumption date after the fact.
    """
    today = utcnow().date()
    backfill(
        start=(today - timedelta(days=days_back)).isoformat(),
        end=(today + timedelta(days=days_ahead)).isoformat(),
        sources="teams,venues,schedule",
        force_refresh=False,
        dry_run=False,
        root=root,
    )


@app.command(name="test-alert")
def test_alert(
    discover_chat: Annotated[
        bool, typer.Option(help="List chat ids that have messaged your bot.")
    ] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Send a test alert through every configured backend.

    Run this before you need it. Finding out that alerting was misconfigured
    during the first outage means the outage already cost you the data it was
    supposed to protect.
    """
    from mlb_edge.alerting import Alert, Severity, TelegramAlerter, build_alerter

    load_settings(root)  # fail early on a broken config
    telegram = TelegramAlerter.from_env()

    if discover_chat:
        if telegram is None:
            console.print(
                "[red]TELEGRAM_BOT_TOKEN is not set[/red] -- needed to look up chat ids"
            )
            raise typer.Exit(code=1)
        chats, note = telegram.discover_chat_ids()
        console.print(note)
        for chat in chats:
            console.print(
                f"  [bold]{chat['id']}[/bold]  {chat.get('type', '?')}  "
                f"{chat.get('title') or ''}"
            )
        if chats:
            console.print(
                f"\nSet [bold]TELEGRAM_CHAT_ID={chats[0]['id']}[/bold] "
                "in /etc/mlb-edge/mlb-edge.env"
            )
        raise typer.Exit(code=0 if chats else 1)

    console.print("[bold]configured backends[/bold]")
    if telegram is None:
        console.print(
            "  [yellow]telegram: NOT configured[/yellow] -- set TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID in /etc/mlb-edge/mlb-edge.env"
        )
    else:
        ok, detail = telegram.check()
        style = "green" if ok else "red"
        console.print(f"  [{style}]telegram: {detail}[/{style}]")

    alerter = build_alerter()
    alert = Alert(
        key="test-alert",
        severity=Severity.INFO,
        subject="mlb-edge test alert",
        body=(
            "If you are reading this on your phone, alerting works. "
            "Sent by `mlb-edge test-alert`."
        ),
    )

    console.print("\n[bold]delivery[/bold]")
    # Deliberately bypasses the throttle: this is a test, and a throttled test
    # that silently sends nothing would be worse than no test at all.
    results = alerter.send_per_backend(alert)
    for description, delivered, detail in results:
        style = "green" if delivered else "red"
        status = "delivered" if delivered else "FAILED"
        console.print(f"  [{style}]{status}[/{style}]  {description}")
        if detail:
            console.print(f"    {detail}")

    reached_phone = any(
        delivered for description, delivered, _ in results if "telegram" in description
    )
    if reached_phone:
        console.print("\n[green]alerting works[/green] -- check your phone")
        return

    console.print(
        "\n[yellow]nothing reached a phone.[/yellow] The journal still has every "
        "alert, but nobody reads the journal at 3am -- which is when the poller "
        "dying costs you a night of closing lines that Tier 0 cannot re-collect."
    )
    raise typer.Exit(code=1)


@app.command(name="poll-status")
def poll_status(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Archive coverage and what the odds budget still buys."""
    from mlb_edge.poll import PollArchive, budget_forecast, season_days_remaining

    settings = load_settings(root)
    archive_root = Path(settings.section("poller").get("archive_dir", "data/poll"))
    archive = PollArchive(
        archive_root if archive_root.is_absolute() else settings.root / archive_root
    )

    stats = archive.stats()
    if not stats:
        console.print("[yellow]archive is empty -- the poller has never written a file[/yellow]")
    else:
        table = Table(title=f"poll archive: {archive.root}")
        table.add_column("venue")
        table.add_column("files", justify="right")
        table.add_column("size", justify="right")
        table.add_column("first")
        table.add_column("last")
        for venue, entry in sorted(stats.items()):
            table.add_row(
                venue,
                f"{entry['files']:,}",
                f"{entry['bytes'] / 1e6:,.1f} MB",
                entry["first"] or "-",
                entry["last"] or "-",
            )
        console.print(table)

    console.print(f"\ndays left in season window: {season_days_remaining(settings)}")

    quota = _latest_quota(archive)
    if quota is None:
        console.print(
            "[yellow]no odds quota observed yet[/yellow] -- the poller reads it from the "
            "API response header on its first successful call"
        )
        return

    forecast = budget_forecast(settings, quota)
    console.print(
        f"odds quota remaining: [bold]{quota:,}[/bold] credits "
        f"({forecast['credits_per_call']} per call = "
        f"{forecast['calls_affordable']:,.0f} calls)"
    )
    sustainable = forecast["sustainable_interval_minutes"]
    configured = forecast["configured_interval_minutes"]
    if sustainable > configured:
        console.print(
            f"[yellow]throttled[/yellow]: sustaining the season needs "
            f"{sustainable:,.0f} min between polls, not the configured {configured:.0f} min. "
            f"At {configured:.0f} min this quota lasts "
            f"{forecast['days_at_configured_interval']:.1f} days."
        )
    else:
        console.print(
            f"[green]quota is ample[/green]: polling at the configured "
            f"{configured:.0f} min ({sustainable:,.0f} min would be sustainable)"
        )


@app.command(name="probe-hydrate")
def probe_hydrate(
    day: Annotated[str | None, typer.Option(help="Date to probe, YYYY-MM-DD. Defaults to today.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Find which StatsAPI hydrate terms are still accepted.

    StatsAPI rejects the WHOLE request with 406 when it does not recognise one
    hydrate term -- it does not ignore the term and return what it can. So one
    stale term in a six-term string returns nothing at all, on a URL that
    worked last season, with an error that says nothing about hydration.

    This tries the bare call, then each term on its own, and prints which to
    remove. Unauthenticated and about eight requests.
    """
    from datetime import date as _date

    from mlb_edge.http import UpstreamError, client_for
    from mlb_edge.hydrate import Verdict, bisect_terms, hydrate_string

    settings = load_settings(root)
    source = settings.source("mlb_statsapi")
    terms = list(source.get("schedule_hydrate") or [])
    when = _date.fromisoformat(day) if day else _date.today()
    client = client_for(settings, "mlb_statsapi")

    def probe(subset: list[str]) -> tuple[bool, int | None, str]:
        if subset:
            url = source.endpoint(
                "schedule",
                start=when.isoformat(),
                end=when.isoformat(),
                hydrate=hydrate_string(subset),
            )
        else:
            url = source.endpoint(
                "schedule_minimal", start=when.isoformat(), end=when.isoformat()
            )
        try:
            response = client.get(url)
        except UpstreamError as exc:
            return False, exc.status, str(exc)
        except Exception as exc:  # noqa: BLE001 - a probe must report, not raise
            return False, None, f"{type(exc).__name__}: {exc}"
        return True, response.status, ""

    console.print(f"probing {len(terms)} hydrate terms against {when.isoformat()}\n")

    # StatsAPI usually names the offending field in the 406 body ("Invalid
    # hydration: X"). One request may answer the question outright, so show it
    # before spending eight more.
    full_ok, full_status, full_detail = probe(terms)
    if not full_ok:
        console.print(f"[red]full hydrate string failed[/red] (HTTP {full_status}):")
        console.print(f"  {full_detail}\n")
    else:
        console.print("[green]the full hydrate string is accepted[/green] -- nothing to fix.\n")

    result = bisect_terms(terms, probe)

    colours = {
        Verdict.KEEP: "green",
        Verdict.STRIP: "yellow",
        Verdict.DROP: "red",
        Verdict.INCONCLUSIVE: "yellow",
    }
    table = Table(title="hydrate terms")
    table.add_column("term as configured")
    table.add_column("status", justify="right")
    table.add_column("base term")
    table.add_column("status", justify="right")
    table.add_column("verdict")
    table.add_row(
        "[dim](none -- bare call)[/dim]",
        "",
        "",
        "",
        "[green]accepted[/green]"
        if result.baseline_ok
        else f"[red]FAILED[/red] {result.baseline_detail}",
    )
    for entry in result.terms:
        # The base column is the whole point: "probablePitcher(note) rejected"
        # is not "probablePitcher rejected", and collapsing the two costs the
        # probable starters.
        same = entry.base == entry.term
        table.add_row(
            entry.term,
            str(entry.status or "-"),
            "[dim]same[/dim]" if same else entry.base,
            "" if entry.base_status is None else str(entry.base_status),
            f"[{colours[entry.verdict]}]{entry.describe()}[/{colours[entry.verdict]}]",
        )
    console.print(table)

    if result.combined_ok is False:
        console.print(
            "\n[yellow]Every term passes alone but the combination fails.[/yellow] "
            "That points at the combination or the URL length, not one term."
        )
    console.print(f"\n{result.suggestion()}")
    if result.stripped or result.rejected:
        corrected = result.corrected_terms()
        console.print(
            "\nThe games spine needs none of these -- the completeness check uses "
            "the unhydrated endpoint and the ingester degrades. What this restores "
            "is the hydrated extras."
        )
        console.print("\nsources.mlb_statsapi.schedule_hydrate should be:")
        for term in corrected:
            console.print(f"  - {term}")
        if not corrected:
            console.print("  [dim](empty -- every term was rejected)[/dim]")
        console.print(f"\nResulting hydrate: [bold]{hydrate_string(corrected) or '(none)'}[/bold]")
        raise typer.Exit(1)


def _latest_quota(archive: Any) -> int | None:
    """Most recent non-null quota_remaining in the odds archive."""
    import polars as pl

    files = archive.files("odds")
    for path in reversed(files):
        frame = pl.read_parquet(path, columns=["quota_remaining"])
        values = frame["quota_remaining"].drop_nulls()
        if len(values):
            return int(values[-1])
    return None


@app.command(name="import-polls")
def import_polls(
    venue: Annotated[str | None, typer.Option(help="Restrict to one venue.")] = None,
    reimport: Annotated[
        bool, typer.Option(help="Re-parse files already imported (after a parser fix).")
    ] = False,
    limit: Annotated[int | None, typer.Option(help="Import at most N files.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Parse the poll archive into the warehouse.

    Incremental by default. Because the archive holds the original bytes, a
    parser or field-map fix applies retroactively to everything ever polled --
    run with ``--reimport`` after making one.
    """
    from mlb_edge.ingest.poll_import import PollImporter

    settings, warehouse, _ = _context(root)
    importer = PollImporter(settings, warehouse)
    report = importer.run(venue=venue, reimport=reimport, limit=limit)
    console.print(report.summary())
    if report.unresolved:
        console.print(
            f"[yellow]{report.unresolved} records unresolved[/yellow] -- usually means the "
            "schedule is stale. Run: mlb-edge backfill --sources teams,venues,schedule "
            "then re-run with --reimport"
        )
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


def main() -> None:
    """Console entry point.

    The wrapper above catches the common case at every command. This catches
    anything that reaches config validation by another route, so no path can
    answer a missing credential with a stack trace.
    """
    try:
        app()
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        console.print(_credentials_hint())
        raise typer.Exit(2) from None


if __name__ == "__main__":
    main()
