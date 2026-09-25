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

import contextlib
import json
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from mlb_edge.config import MISSING_SECRET, ConfigError, Settings
from mlb_edge.config import load_settings as _load_settings_raw
from mlb_edge.eval.adverse import MAX_PLAUSIBLE_GAP
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

    state = backup_module.BackupState.load(_backup_state_path(settings))
    state.record_backup(now=utcnow())

    command = str(config.get("push_command") or "").strip()
    if push and command:
        pushed, output = backup_module.push(backup_dir=destination, command=command)
        state.record_push(now=utcnow(), ok=pushed, error=None if pushed else output)
        state.save()
        if pushed:
            console.print("[green]pushed off-box[/green]")
        else:
            # Loud: a local-only backup does not survive the failure it exists for.
            console.print(f"[red]push FAILED[/red] -- this backup is local only\n{output}")
            raise typer.Exit(code=1)
    elif push:
        # Exit non-zero. This printed yellow and exited 0 for 23 days, so the
        # systemd timer stayed green while the only irreplaceable asset in the
        # project existed in exactly one place. `--no-push` is how you say you
        # meant it; there is no longer a way to mean it by accident.
        state.save()
        console.print(
            "[red]no push_command configured[/red] -- this backup is LOCAL ONLY and "
            "does not survive the droplet dying.\n"
            "Set backup.push_command in config/local.yaml (see deploy/README.md), "
            "or pass --no-push if local-only is deliberate."
        )
        raise typer.Exit(code=2)
    else:
        state.save()

    # Past this point the bytes are safely off-box, so nothing below may change
    # the exit code. Same ordering the poller uses for its archive writes:
    # the thing that matters happens first, and the housekeeping after it is not
    # allowed to overrule it.
    removed, problems = backup_module.prune(
        backup_root, keep=int(config.get("keep_local", 3))
    )
    if removed:
        console.print(f"pruned {len(removed)} older local backups")
    for line in problems:
        console.print(
            f"[yellow]WARN[/yellow] could not prune {line}\n"
            "  The backup itself succeeded. Old local copies are using disk; "
            "check ownership under the backup directory."
        )


def _backup_state_path(settings: Settings) -> Path:
    """Beside the archive, so a restart does not reset the clock on it."""
    archive_root = Path(settings.section("poller").get("archive_dir", "data/poll"))
    root = archive_root if archive_root.is_absolute() else settings.root / archive_root
    return root / str(settings.section("backup").get("state_file", "backup_state.json"))


@backup_app.command("status")
def backup_status(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """When the archive last left this box.

    The only question that matters about a backup. "A backup ran" and "a copy
    exists somewhere else" are different facts, and only the second one survives
    the droplet.
    """
    from mlb_edge import backup as backup_module
    from mlb_edge import pollhealth

    settings = load_settings(root)
    state = backup_module.BackupState.load(_backup_state_path(settings))

    console.print(f"last local backup: {state.last_backup_at or '[red]never[/red]'}")
    age = state.off_box_age(utcnow())
    if age is None:
        console.print("last off-box push: [red]never[/red]")
    else:
        console.print(
            f"last off-box push: {state.last_push_at}  "
            f"({age.total_seconds() / 3600:.1f}h ago)"
        )
    if state.last_push_error:
        console.print(f"last push error:   [red]{state.last_push_error}[/red]")

    max_age = timedelta(hours=float(settings.section("backup").get("max_age_hours", 48)))
    alerts = pollhealth.backup_alerts(state, max_age=max_age)
    if not alerts:
        console.print("\n[green]OK[/green] a copy exists off this box")
        return
    for alert in alerts:
        console.print(f"\n[red]{alert.subject}[/red]\n{alert.body}")
    raise typer.Exit(code=1)


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
    from mlb_edge.poll import NoSourcesEnabled, PollDaemon

    settings = load_settings(root)
    try:
        daemon = PollDaemon(settings)
        daemon.install_signal_handlers()
        daemon.run(once=once, max_ticks=max_ticks)
    except NoSourcesEnabled as exc:
        # Exit non-zero so `systemctl status` shows a failure. Restart=always
        # still brings it back -- credentials may arrive later -- but the unit
        # no longer looks healthy while polling nothing.
        console.print(f"[red]poller cannot start:[/red] {exc}")
        raise typer.Exit(78) from None


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


@app.command(name="explain-coverage")
def explain_coverage(
    venue: Annotated[str, typer.Option(help="Venue to explain, e.g. kalshi or odds.")] = "kalshi",
    day: Annotated[str | None, typer.Option(help="Slate date, YYYY-MM-DD. Defaults to today.")] = None,
    labels: Annotated[int, typer.Option(help="How many payload strings to print.")] = 40,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Why a venue's coverage is short: not counted, or not fetched?

    `captured 1/14` does not distinguish a matcher that failed to recognise
    titles -- a counting bug, no data lost -- from a board that was never
    fetched, which is permanent loss on a source with no historical endpoint.
    Same log line, opposite responses.

    Reads the newest archived tick off disk. No network, so it is safe to run
    against a live poller.
    """
    from datetime import date as _date

    import polars as pl

    from mlb_edge.completeness import (
        SlateCache,
        coverage_for_venue,
        diagnose_coverage,
        extract_labels,
        games_in_window,
        unmatched_tickers,
    )
    from mlb_edge.poll import PollArchive

    settings = load_settings(root)
    poller_config = settings.section("poller")
    archive_root = Path(poller_config.get("archive_dir", "data/poll"))
    archive = PollArchive(
        archive_root if archive_root.is_absolute() else settings.root / archive_root
    )

    files = archive.files(venue)
    if not files:
        console.print(f"[yellow]no archived ticks for {venue}[/yellow]")
        raise typer.Exit(1)

    newest = files[-1]
    frame = pl.read_parquet(newest, columns=["payload", "error"])
    payloads = [p for p in frame["payload"].to_list() if p]
    console.print(f"tick: [bold]{newest.name}[/bold]  ({len(payloads)} payloads)\n")

    when = _date.fromisoformat(day) if day else _date.today()
    # Low-retry: this is a diagnostic and the default five-attempt ladder makes
    # it sit for a minute before saying the schedule is unreachable.
    from mlb_edge.http import HttpClient

    schedule_client = HttpClient(
        user_agent=settings.section("http").get("user_agent", "mlb-edge/0.1"),
        timeout_seconds=float(poller_config.get("schedule_timeout_seconds", 10)),
        max_attempts=int(poller_config.get("schedule_max_attempts", 2)),
        backoff_initial_seconds=1.0,
        backoff_max_seconds=4.0,
        rate_limit_per_minute=60,
    )
    slate = SlateCache(settings, schedule_client).slate_for(when)
    games = games_in_window(
        list(slate.games),
        lead=timedelta(hours=float(poller_config.get("completeness_slate_lead_hours", 12))),
        trail=timedelta(hours=float(poller_config.get("completeness_slate_trail_hours", 5))),
    )
    if not games:
        console.print("[yellow]no games in the window to compare against[/yellow]")
        raise typer.Exit(1)

    evidence = diagnose_coverage(
        venue,
        payloads,
        games,
        quote_horizon=timedelta(
            hours=float(poller_config.get("completeness_quote_horizon_hours", 6))
        ),
        closes_after=timedelta(
            hours=float(poller_config.get("completeness_quote_close_hours", 4))
        ),
    )

    method = evidence[0].method if evidence else "team-mention"
    table = Table(
        title=f"{venue}: {sum(e.matched for e in evidence)}/{len(evidence)} counted "
        f"(joined on {method})"
    )
    table.add_column("game")
    table.add_column("first pitch", justify="right")
    # The column must describe the matcher that actually ran. Showing fuzzy
    # tokens beside a ticker join sends you to debug a dead code path.
    table.add_column(
        {"ticker": "tickers on the board", "exact": "event joined"}.get(
            method, "tokens searched"
        )
    )
    table.add_column("diagnosis")
    for entry in sorted(
        evidence,
        key=lambda e: (
            e.matched,
            e.not_yet_expected or e.no_longer_expected or e.not_listed,
            e.game.label,
        ),
    ):
        if entry.matched:
            colour = "green"
        elif entry.not_yet_expected or entry.no_longer_expected or entry.not_listed:
            colour = "cyan"
        elif entry.ticker_candidates or entry.loose_hits:
            colour = "yellow"
        else:
            colour = "red"
        if method == "ticker":
            attempted = "\n".join(entry.ticker_candidates) or "[dim]none[/dim]"
        elif method == "exact":
            # Exact venues join on the event, never on tokens. Showing an empty
            # token list here described a matcher that does not run.
            attempted = entry.event_match or "[dim]no event[/dim]"
        else:
            attempted = ", ".join(sorted(entry.strict_tokens)) or "[dim]none[/dim]"
        table.add_row(
            entry.game.label,
            f"{entry.hours_to_first_pitch:+.1f}h",
            attempted,
            f"[{colour}]{entry.diagnosis}[/{colour}]",
        )
    console.print(table)

    pending = [e for e in evidence if e.not_yet_expected and not e.matched]
    if pending:
        console.print(
            f"\n[cyan]{len(pending)} game(s) are not yet expected[/cyan] -- more than "
            "the quote horizon from first pitch. Not a shortfall; books post a "
            "late game's market closer to the start."
        )
    over = [e for e in evidence if e.no_longer_expected and not e.matched]
    if over:
        console.print(
            f"\n[cyan]{len(over)} game(s) have finished[/cyan] -- their markets settled "
            "and left the board. Not a shortfall; Kalshi is polled with status=open, "
            "so a settled game's tickers stop appearing."
        )

    codes = coverage_for_venue(venue, payloads, games).unmapped_codes
    if codes:
        console.print(
            f"\n[red]ticker codes not in the alias table: {', '.join(codes)}[/red]\n"
            "Each one is a game that cannot be counted, and a one-line fix in "
            "mlb_edge.kalshi_tickers.TEAM_ALIASES."
        )

    if method == "ticker":
        orphans = unmatched_tickers(payloads, games)
        if orphans:
            console.print(
                f"\ntickers that joined to no game on this slate ({len(orphans)}):"
            )
            for line in orphans[:12]:
                console.print(f"  {line}")
            console.print(
                "[dim]A game with no ticker and a ticker with no game are usually "
                "the same fact seen from two sides.[/dim]"
            )

    unresolved = [
        e for e in evidence
        if not e.matched
        and not e.not_yet_expected
        and not e.no_longer_expected
        and not e.not_listed
    ]
    absent = [e for e in unresolved if not e.present]
    gaps = [e for e in unresolved if e.present]
    if gaps:
        console.print(
            f"\n[yellow]{len(gaps)} game(s) are in the payload but not counted.[/yellow] "
            "That is a matcher gap, not data loss -- the archive has them."
        )
    if absent:
        console.print(
            f"\n[red]{len(absent)} game(s) do not appear at all.[/red] "
            "That is real loss on a source with no historical endpoint."
        )

    # Which slate dates the tick actually covers. Asked in review: every string
    # in a sample was three days out, which could equally be sample truncation
    # or a board holding only forward-dated markets. Counting the tickers
    # settles it, and "today: 0" is a different problem from "today: 14".
    from collections import Counter

    from mlb_edge.kalshi_tickers import parse_ticker, tickers_from_payloads

    parsed = [parse_ticker(tk) for tk in tickers_from_payloads(payloads)]
    dates = Counter(p.game_date.isoformat() for p in parsed if p is not None)
    if dates:
        console.print("\ngame dates in this tick:")
        for day_str, count in sorted(dates.items()):
            marker = "  <- the slate being checked" if day_str == when.isoformat() else ""
            console.print(f"  {day_str}  {count:>4} tickers{marker}")
        if when.isoformat() not in dates:
            console.print(
                f"\n[red]no tickers for {when.isoformat()} at all.[/red] "
                "The board in this tick is entirely forward-dated -- that is a "
                "fetch problem, not a matching one."
            )

    strings = extract_labels(payloads)
    console.print(f"\npayload strings ({len(strings)} distinct, showing {min(labels, len(strings))}):")
    for value in strings[:labels]:
        console.print(f"  {value}")
    if not strings:
        console.print(
            "  [yellow]none[/yellow] -- no recognisable title or ticker keys. "
            "The payload shape is not what the matcher assumes; that alone "
            "explains the shortfall."
        )


@app.command(name="probe-billing")
def probe_billing(
    root: Annotated[Path | None, typer.Option()] = None,
    confirm: Annotated[bool, typer.Option(help="Actually spend the credits.")] = False,
) -> None:
    """Measure what a request actually costs, and how big the plan really is.

    The Odds API documents `bookmakers` as billing one region-equivalent per
    ten bookmakers, which would give the full five-book consensus for the price
    of one region. Documentation is a claim; the response header is the fact.
    This makes one call of each shape and reads the `x-requests-remaining`
    delta, so the change is wired in against a measurement rather than a doc.

    It also answers a question nobody asked: `x-requests-used` plus
    `x-requests-remaining` is the plan size for the period, which is worth
    knowing independently of what the config believes.

    Costs about 3 credits. Requires --confirm, because a diagnostic that spends
    from a budget this tight should be deliberate.
    """
    from mlb_edge.http import UpstreamError, client_for

    settings = load_settings(root)
    source = settings.source("odds")
    client = client_for(settings, "odds")
    sport = source.get("sport_key", "baseball_mlb")
    odds_url = source.endpoint("odds", sport=sport)
    base = source.base_url

    def quota(url: str, params: dict[str, Any]) -> tuple[int | None, int | None, str]:
        try:
            response = client.get(url, params=params)
        except UpstreamError as exc:
            return None, None, f"HTTP {exc.status}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            return None, None, f"{type(exc).__name__}: {exc}"
        headers = {k.lower(): v for k, v in response.headers.items()}

        def as_int(name: str) -> int | None:
            try:
                return int(headers[name])
            except (KeyError, TypeError, ValueError):
                return None

        return as_int("x-requests-remaining"), as_int("x-requests-used"), ""

    key = source.require("api_key")

    # /sports is documented as free. Reading the quota through it costs nothing
    # and establishes the baseline the two paid calls are measured against.
    remaining, used, error = quota(f"{base}/sports", {"apiKey": key})
    if error:
        console.print(f"[red]could not read the quota:[/red] {error}")
        raise typer.Exit(1)
    console.print(f"quota now: remaining={remaining:,} used={used:,}")
    if remaining is not None and used is not None:
        console.print(
            f"[bold]plan size for this period: {remaining + used:,} credits[/bold]  "
            f"(config believes {source.get('monthly_request_budget')})"
        )
    if not confirm:
        console.print(
            "\n[yellow]stopping here.[/yellow] Re-run with --confirm to spend ~3 "
            "credits measuring what each request shape actually bills."
        )
        return

    consensus = [
        b["key"]
        for b in settings.books.get("books", [])
        if settings.books.get("consensus", {}).get("weights", {}).get(b.get("key"), 0)
    ]
    shapes = [
        ("regions=us,eu, h2h  (current)", {"regions": "us,eu", "markets": "h2h"}),
        ("regions=eu, h2h", {"regions": "eu", "markets": "h2h"}),
    ]
    if consensus:
        shapes.append(
            (
                f"bookmakers={len(consensus)} consensus books, h2h",
                {"bookmakers": ",".join(consensus), "markets": "h2h"},
            )
        )

    table = Table(title="measured cost per call")
    table.add_column("request shape")
    table.add_column("credits", justify="right")
    table.add_column("note")

    previous = remaining
    for label, extra in shapes:
        after, _, err = quota(odds_url, {"apiKey": key, "oddsFormat": "american", **extra})
        if err or after is None or previous is None:
            table.add_row(label, "-", f"[red]{err or 'no header'}[/red]")
            continue
        spent = previous - after
        table.add_row(label, str(spent), "")
        previous = after
    console.print(table)
    console.print(
        "\nIf the bookmakers row bills more than 1, the documented "
        "one-region-per-ten-bookmakers rule does not hold for this plan. "
        "Report the number rather than wiring the change in."
    )


@app.command(name="probe-ratelimit")
def probe_ratelimit(
    venue: Annotated[str, typer.Option(help="Source to probe, e.g. kalshi.")] = "kalshi",
    confirm: Annotated[bool, typer.Option(help="Actually send the bursts.")] = False,
    ceiling: Annotated[int, typer.Option(help="Highest rate to try, req/min.")] = 600,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Measure the real request rate a venue allows, rather than inheriting ours.

    `rate_limit_per_minute` is a number this repo picked. So was `tier: 0`, and
    that one cost 25 days of h2h-only polling on a plan that paid for more. A
    guess that the code then obeys as though it were the venue's rule is not a
    safety margin, it is an invented constraint -- and this one bounds how fast
    the slower leg of the whole measurement can run.

    Ramps in steps and stops at the first 429, reporting the highest rate that
    stayed clean. Reads any `x-ratelimit-*` headers first, which is free.

    Run it between slates if you can. It sends real requests on the production
    key, and tripping a limit during a live slate costs archive coverage that
    cannot be re-fetched.
    """
    import time as _time

    from mlb_edge.http import HttpClient, UpstreamError

    settings = load_settings(root)
    source = settings.source(venue)
    configured = int(source.get("rate_limit_per_minute", 0) or 0)
    endpoints = source.get("endpoints") or {}
    # A cheap, idempotent, unauthenticated endpoint if one exists.
    path = endpoints.get("exchange_status") or endpoints.get("status") or next(
        (v for k, v in endpoints.items() if "{" not in str(v)), None
    )
    if not path:
        console.print(f"[red]no parameter-free endpoint for {venue}[/red]")
        raise typer.Exit(1)
    url = f"{source.base_url}{path}"
    console.print(f"probing [bold]{venue}[/bold] at {url}")
    console.print(f"configured rate_limit_per_minute: [bold]{configured}[/bold]\n")

    # One request, no rate limiting of our own, to read whatever the venue says
    # about its own limits. Free information, and often conclusive.
    probe_client = HttpClient(
        user_agent=settings.section("http").get("user_agent", "mlb-edge/0.1"),
        timeout_seconds=10.0,
        max_attempts=1,
        rate_limit_per_minute=0,
    )
    try:
        response = probe_client.get(url)
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        console.print(f"[red]could not reach {venue}:[/red] {exc}")
        raise typer.Exit(1) from None

    advertised = {
        k: v for k, v in response.headers.items() if "ratelimit" in k.lower()
    }
    if advertised:
        console.print("[green]the venue advertises its limits:[/green]")
        for k, v in sorted(advertised.items()):
            console.print(f"  {k}: {v}")
        console.print("\nUse these rather than the ramp below if they are clear.")
    else:
        console.print("[dim]no x-ratelimit-* headers; the ramp is the only source[/dim]")

    if not confirm:
        console.print(
            "\n[yellow]stopping here.[/yellow] Re-run with --confirm to ramp "
            "request rates until the venue pushes back."
        )
        return

    table = Table(title=f"{venue}: measured rate ceiling")
    table.add_column("req/min", justify="right")
    table.add_column("sent", justify="right")
    table.add_column("429s", justify="right")
    table.add_column("result")

    best_clean = 0
    for rate in (30, 60, 120, 240, 480, 960):
        if rate > ceiling:
            break
        burst, throttled, other = 12, 0, 0
        gap = 60.0 / rate
        started = _time.monotonic()
        for index in range(burst):
            try:
                probe_client.get(url)
            except UpstreamError as exc:
                if exc.status == 429:
                    throttled += 1
                else:
                    other += 1
            except Exception:  # noqa: BLE001 - a probe reports, never raises
                other += 1
            # Pace against the wall clock from the burst's start rather than
            # sleeping a fixed gap, so request time does not slow the burst
            # below the rate being tested. Testing 240/min at an actual 180
            # would report a ceiling that was never reached.
            due = started + gap * (index + 1)
            _time.sleep(max(0.0, due - _time.monotonic()))
        if throttled:
            table.add_row(str(rate), str(burst), str(throttled), "[red]throttled[/red]")
            console.print(table)
            console.print(
                f"\n[bold]Highest clean rate: {best_clean} req/min.[/bold] "
                f"Configured is {configured}."
            )
            console.print(
                "Set rate_limit_per_minute below the clean rate, not at it -- the "
                "ceiling is shared with whatever else uses this key."
            )
            return
        note = "[green]clean[/green]" if not other else f"[yellow]{other} other errors[/yellow]"
        table.add_row(str(rate), str(burst), "0", note)
        best_clean = rate

    console.print(table)
    console.print(
        f"\n[bold]No throttling up to {best_clean} req/min[/bold] "
        f"(configured: {configured}). The ceiling is above what was tried; "
        "raise --ceiling to find it, or stop here if this is already enough."
    )


#: Container keys a Kalshi orderbook has been seen under, newest first.
#: MEASURED 2026-09-24 from 464,917 archived payloads: the live shape is
#: ``{"orderbook_fp": {"yes_dollars": [["0.5400","3012.00"], ...],
#:                     "no_dollars":  [["0.0200","7002.00"], ...]}}``
#: -- price/size pairs as decimal STRINGS, split by side, nested under
#: ``orderbook_fp``. The earlier reader assumed ``orderbook`` with numeric
#: pairs, found nothing, and reported zero snapshots rather than an unknown
#: shape. The alternatives are kept because one shape change already happened.
ORDERBOOK_CONTAINERS = ("orderbook_fp", "orderbook", "book")


def _side_of(key: Any) -> str | None:
    """``yes``/``no`` for a side key, whatever suffix it carries."""
    lowered = str(key).lower()
    if lowered.startswith("yes"):
        return "yes"
    if lowered.startswith("no"):
        return "no"
    return None


@dataclass(frozen=True)
class OrderbookSnapshot:
    """One archived book, parsed. ``shape`` names which container matched."""

    shape: str
    levels: dict[str, int]
    contracts: dict[str, float]

    @property
    def deepest(self) -> int:
        return max(self.levels.values(), default=0)

    @property
    def total_contracts(self) -> float:
        return sum(self.contracts.values())


def _parse_orderbook(payload: str) -> OrderbookSnapshot | None:
    """Parse a Kalshi orderbook payload, tolerating the shapes we have seen.

    Returns ``None`` when nothing recognisable is found -- deliberately not an
    empty book, because "we could not read this" and "this book is empty" are
    different facts and conflating them is what cost a run.
    """
    import json as _json

    try:
        data = _json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    for container in (*ORDERBOOK_CONTAINERS, None):
        node = data.get(container) if container else data
        if not isinstance(node, dict):
            continue
        # At the root there is no container name vouching for the shape, so a
        # side key alone is not enough: `{"note": "..."}` starts with "no" and
        # would otherwise parse as an empty book, quietly turning arbitrary
        # JSON into a zero-level snapshot in the denominator.
        if container is None and not any(
            isinstance(v, list) and _side_of(k) for k, v in node.items()
        ):
            continue
        levels: dict[str, int] = {}
        contracts: dict[str, float] = {}
        for key, value in node.items():
            side = _side_of(key)
            if side is None:
                continue
            if not isinstance(value, list):
                # A recognised container with a null side is an EMPTY book, not
                # an unreadable one. Kalshi sends null for a side with no
                # resting orders, and treating that as unparseable would drop
                # real empty books out of the denominator.
                levels.setdefault(side, 0)
                contracts.setdefault(side, 0.0)
                continue
            size = 0.0
            counted = 0
            for entry in value:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                try:
                    size += float(entry[1])
                except (TypeError, ValueError):
                    continue
                counted += 1
            levels[side] = counted
            contracts[side] = size
        if levels:
            return OrderbookSnapshot(
                shape=container or "(root)", levels=levels, contracts=contracts
            )
    return None


def _orderbook_levels(payload: str) -> tuple[int, int] | None:
    """``(yes levels, no levels)``. Thin view over :func:`_parse_orderbook`."""
    book = _parse_orderbook(payload)
    if book is None:
        return None
    return book.levels.get("yes", 0), book.levels.get("no", 0)


@app.command(name="probe-depth")
def probe_depth(
    depth: Annotated[int, typer.Option(help="Depth to request when probing live.")] = 100,
    live: Annotated[bool, typer.Option(help="Also ask Kalshi what it serves.")] = False,
    ticks: Annotated[int, typer.Option(help="How many archived ticks to scan. 0 = all.")] = 0,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """What the archive actually holds, level by level, and what Kalshi serves.

    `orderbook_depth: 10` is a number this repo chose and nothing has checked.
    The cost of it being low is not recoverable: there is no historical
    orderbook endpoint, and realised depth at the dislocated price is the
    measurement stage two exists for.

    The first version of this reported a bare count, so a zero could equally
    mean "no orderbook rows were ever written" or "rows are there and this
    cannot read them". Those need opposite responses, which is precisely the
    defect this probe was written to find, one level up. It now reports the
    breakdown that separates them.
    """
    import polars as pl

    from mlb_edge.poll import PollArchive

    settings = load_settings(root)
    configured = int(settings.source("kalshi").get("orderbook_depth", 10))
    console.print(f"configured orderbook_depth: [bold]{configured}[/bold]\n")

    poller_config = settings.section("poller")
    archive_root = Path(poller_config.get("archive_dir", "data/poll"))
    archive = PollArchive(
        archive_root if archive_root.is_absolute() else settings.root / archive_root
    )
    files = archive.files("kalshi")
    if not files:
        console.print("[yellow]no kalshi archive to scan[/yellow]")
    else:
        scanned = files if ticks <= 0 else files[-ticks:]
        console.print(
            f"scanning {len(scanned):,} of {len(files):,} ticks: "
            f"{scanned[0].name} .. {scanned[-1].name}"
        )

        from collections import Counter

        by_endpoint: Counter[str] = Counter()
        levels: Counter[int] = Counter()
        shapes: Counter[str] = Counter()
        contracts: list[float] = []
        with_payload = parsed = 0
        unparsed_samples: list[str] = []

        for path in scanned:
            frame = pl.read_parquet(path, columns=["endpoint", "payload", "error"])
            for endpoint, payload, error in zip(
                frame["endpoint"].to_list(),
                frame["payload"].to_list(),
                frame["error"].to_list(),
                strict=False,
            ):
                label = str(endpoint or "?") + ("" if not error else " (error)")
                by_endpoint[label] += 1
                if endpoint != "orderbook" or error:
                    continue
                if not payload:
                    continue
                with_payload += 1
                book = _parse_orderbook(payload)
                if book is None:
                    if len(unparsed_samples) < 3:
                        unparsed_samples.append(str(payload)[:240])
                    continue
                parsed += 1
                shapes[book.shape] += 1
                levels[book.deepest] += 1
                contracts.append(book.total_contracts)

        # This breakdown is the whole point: it says which of the two states
        # a zero means, without needing another round trip to find out.
        shape = Table(title="rows by endpoint")
        shape.add_column("endpoint")
        shape.add_column("rows", justify="right")
        for label, n in by_endpoint.most_common():
            shape.add_row(label, f"{n:,}")
        console.print(shape)

        console.print(
            f"\norderbook rows with a payload: [bold]{with_payload:,}[/bold]"
            f"   parsed as a book: [bold]{parsed:,}[/bold]"
        )

        if with_payload and not parsed:
            console.print(
                "\n[red]Rows exist and none parse.[/red] The payload is not the "
                "shape this probe expects, so the archive may be fine and the "
                "READER is wrong. Samples:"
            )
            for sample in unparsed_samples:
                console.print(f"  {sample}")
        elif not with_payload:
            console.print(
                "\n[red]No orderbook rows with payloads in this window.[/red]\n"
                "Either the orderbook fetch is not running -- check whether the "
                "markets response yields tickers -- or this window is entirely "
                "between slates, when Kalshi has no open markets to book. "
                "Re-run with --ticks 0 over the whole archive before concluding."
            )
        else:
            if len(shapes) > 1:
                console.print(
                    "[yellow]more than one payload shape in the archive:[/yellow] "
                    + ", ".join(f"{k}={v:,}" for k, v in shapes.most_common())
                )
            else:
                console.print(f"payload shape: [bold]{next(iter(shapes))}[/bold]")

            table = Table(title="levels per snapshot (deepest side), whole archive")
            table.add_column("levels", justify="right")
            table.add_column("snapshots", justify="right")
            table.add_column("share", justify="right")
            table.add_column("", justify="left")
            for lv in sorted(levels):
                share = levels[lv] / parsed * 100
                bar = "#" * int(share / 2)
                mark = "  <- at the cap" if lv >= configured else ""
                table.add_row(f"{lv}{mark}", f"{levels[lv]:,}", f"{share:.1f}%", bar)
            console.print(table)

            saturated = sum(n for lv, n in levels.items() if lv >= configured)
            pct = saturated / parsed * 100
            modal = max(levels, key=lambda lv: levels[lv])
            console.print(
                f"\nmodal depth: [bold]{modal}[/bold] levels    "
                f"at the cap: [bold]{saturated:,} of {parsed:,} ({pct:.1f}%)[/bold]"
            )
            if modal >= configured or pct > 50:
                console.print(
                    f"[red]The cap is binding.[/red] Most books reach level "
                    f"{configured} and stop, which is what a truncated book looks "
                    "like. Depth beyond it was never recorded and cannot be "
                    "fetched back -- raise orderbook_depth now, then confirm with "
                    "--live what Kalshi actually serves."
                )
            elif pct < 5:
                console.print(
                    f"[green]The cap is not binding.[/green] Books are genuinely "
                    f"thinner than {configured} levels, so orderbook_depth was "
                    "never the constraint and little or nothing has been lost."
                )
            else:
                console.print(
                    f"[yellow]The cap binds on {pct:.1f}% of snapshots.[/yellow] "
                    "Real but partial loss, concentrated in the deepest books -- "
                    "which are the ones that matter most for size."
                )

            if contracts:
                ordered = sorted(contracts)
                def pctile(q: float) -> float:
                    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]

                console.print(
                    "\n[bold]resting size per snapshot, both sides "
                    "(this is stage two's depth measurement):[/bold]"
                )
                console.print(
                    f"  median {pctile(0.5):>10,.0f} contracts    "
                    f"p25 {pctile(0.25):>10,.0f}    p75 {pctile(0.75):>10,.0f}    "
                    f"p95 {pctile(0.95):>10,.0f}"
                )

    if not live:
        console.print(
            "\n[yellow]archive scan only.[/yellow] Re-run with --live to ask "
            "Kalshi what it serves."
        )
        return

    # --- what Kalshi actually serves ---------------------------------------
    from mlb_edge.poll import KalshiPoller, _tickers_from

    poller = KalshiPoller(settings)
    config = settings.source("kalshi")
    endpoints = config.get("endpoints") or {}
    series = (config.get("series_tickers") or ["KXMLBGAME"])[0]

    # One markets page for a handful of tickers. The first version called
    # poller.poll() here, which sweeps the entire board -- hundreds of requests
    # and minutes of rate-limited waiting -- to obtain five strings.
    markets_path = endpoints["markets"]
    try:
        response = poller._client.get(
            f"{config.base_url}{markets_path}",
            params={"series_ticker": series, "status": "open", "limit": 20},
            headers=poller._headers(markets_path),
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        console.print(f"[red]could not list markets:[/red] {exc}")
        raise typer.Exit(1) from None

    tickers = _tickers_from(response.text())[:5]
    if not tickers:
        console.print(
            f"[red]no open markets under {series}.[/red]\n"
            "That alone explains an empty archive scan: with no tickers there "
            "are no orderbooks to fetch. Re-run during a slate."
        )
        raise typer.Exit(1)
    console.print(f"\nprobing {len(tickers)} tickers: {', '.join(tickers)}")

    comparison = Table(title=f"levels returned: depth={configured} vs depth={depth}")
    comparison.add_column("ticker")
    comparison.add_column(f"at {configured}", justify="right")
    comparison.add_column(f"at {depth}", justify="right")
    comparison.add_column("verdict")

    deeper = 0
    path_template = endpoints["orderbook"]
    for ticker in tickers:
        path = path_template.format(ticker=ticker)
        url = f"{config.base_url}{path}"
        counts: list[int] = []
        for requested in (configured, depth):
            try:
                reply = poller._client.get(
                    url, params={"depth": requested}, headers=poller._headers(path)
                )
                got = _orderbook_levels(reply.text())
                counts.append(max(got) if got else 0)
            except Exception as exc:  # noqa: BLE001
                counts.append(-1)
                console.print(f"[dim]{ticker}: {exc}[/dim]")
        shallow, deep = counts
        if deep > shallow:
            deeper += 1
            verdict = f"[red]TRUNCATING -- {deep - shallow} levels lost[/red]"
        elif shallow < configured:
            verdict = "[dim]book shallower than the cap; inconclusive[/dim]"
        else:
            verdict = "[green]cap not binding[/green]"
        comparison.add_row(ticker, str(shallow), str(deep), verdict)
    console.print(comparison)

    if deeper:
        console.print(
            f"\n[bold red]orderbook_depth: {configured} is truncating "
            f"{deeper} of {len(tickers)} books.[/bold red]\n"
            "Every snapshot archived so far is capped at the old value "
            "permanently: there is no historical orderbook endpoint."
        )
        raise typer.Exit(1)
    console.print(
        "\n[green]No book returned more levels than the cap allows.[/green] "
        "Either the cap is above what these books hold, or Kalshi caps here. "
        "Re-run against a busier slate before treating it as settled."
    )


def _resident_mb() -> float:
    """Resident memory of this process, MB. Linux only; 0.0 elsewhere."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def _available_mb() -> float:
    """MemAvailable, MB. What the kernel thinks can be handed out without swap."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


@app.command(name="adverse-selection")
@app.command("probe-inplay")
def probe_inplay(
    reference: Annotated[str, typer.Option(help="Book whose in-play coverage decides it.")] = "pinnacle",
    game_minutes: Annotated[float, typer.Option(help="How long a game is assumed to last.")] = 180.0,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Does the odds feed carry prices after first pitch?

    Settles a structural question with the archive already on disk: whether the
    market-vs-market trade can ever operate in-play, or is pre-game only by
    construction. Reads no network and costs nothing.

    Three outcomes, and they mean different things:

    * **The event vanishes from the feed at first pitch.** The trade is
      pre-game only. Kalshi's in-play board stays recorded but has no
      reference price, and only a model can supply one.
    * **The event stays but the sharp book drops out.** Same conclusion for
      that book; a different book may still quote, at a different price
      quality.
    * **In-play quotes exist.** The limit is not structural, and the question
      becomes what the alignment term costs when a line moves on every pitch.
    """
    import json as _json
    from collections import defaultdict

    import polars as pl

    from mlb_edge.poll import PollArchive
    from mlb_edge.timeutil import parse_iso_utc

    settings = load_settings(root)
    poller_config = settings.section("poller")
    archive_root = Path(poller_config.get("archive_dir", "data/poll"))
    archive = PollArchive(
        archive_root if archive_root.is_absolute() else settings.root / archive_root
    )

    events_seen = 0
    events_after = 0
    fetch_times: set[Any] = set()
    starts: dict[tuple[str, str, Any], Any] = {}
    past_commence: dict[str, int] = defaultdict(int)
    latest_by_book: dict[str, float] = defaultdict(float)
    latest_priced: dict[str, float] = defaultdict(float)
    priced_after: dict[str, int] = defaultdict(int)
    quotes_after: dict[str, int] = defaultdict(int)
    quotes_before: dict[str, int] = defaultdict(int)
    max_minutes = 0.0

    files = archive.files("odds")
    if not files:
        console.print("[red]no odds files in the archive.[/red]")
        raise typer.Exit(1)

    for number, path in enumerate(files, 1):
        frame = pl.read_parquet(path, columns=["payload", "error", "fetched_at"])
        for payload, error, at in zip(
            frame["payload"].to_list(), frame["error"].to_list(),
            frame["fetched_at"].to_list(), strict=False,
        ):
            if error or not payload:
                continue
            try:
                events = _json.loads(payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                raw = event.get("commence_time")
                if not raw:
                    continue
                try:
                    start = parse_iso_utc(str(raw))
                except (ValueError, TypeError):
                    continue
                events_seen += 1
                fetch_times.add(at)
                starts.setdefault(
                    (str(event.get("home_team")), str(event.get("away_team")), start),
                    start,
                )
                minutes = (at - start).total_seconds() / 60.0
                after = minutes > 0
                if after:
                    events_after += 1
                    max_minutes = max(max_minutes, minutes)
                for bookmaker in event.get("bookmakers") or []:
                    if not isinstance(bookmaker, dict):
                        continue
                    key = str(bookmaker.get("key"))
                    market = next(
                        (
                            m for m in bookmaker.get("markets") or []
                            if isinstance(m, dict) and m.get("key") == "h2h"
                        ),
                        None,
                    )
                    if market is None:
                        continue
                    # LISTED is not PRICED. A market can stay on the board
                    # after first pitch with its outcomes suspended or empty,
                    # and counting those as in-play coverage is the same defect
                    # as probe-depth counting rows it could not read. The
                    # study's own filter requires two outcomes with prices, so
                    # the probe applies exactly that test and reports both.
                    priced = sum(
                        1 for o in market.get("outcomes") or []
                        if isinstance(o, dict) and o.get("name") and o.get("price")
                    ) >= 2
                    if after:
                        quotes_after[key] += 1
                        latest_by_book[key] = max(latest_by_book[key], minutes)
                        if priced:
                            priced_after[key] += 1
                            latest_priced[key] = max(latest_priced[key], minutes)
                            if key == reference:
                                past_commence[_minute_band(minutes)] += 1
                    else:
                        quotes_before[key] += 1
        if number % 100 == 0 or number == len(files):
            console.print(f"  {number:,}/{len(files):,} files", highlight=False)

    console.print(
        f"\nevent rows: [bold]{events_seen:,}[/bold]   "
        f"after first pitch: [bold]{events_after:,}[/bold] "
        f"({events_after / events_seen * 100:.1f}%)" if events_seen else "no events"
    )
    if events_after:
        console.print(
            f"latest an event was seen: [bold]{max_minutes:.0f} minutes[/bold] "
            "past its commence_time"
        )

    table = Table(title="h2h after first pitch, by book: listed vs actually priced")
    for column in (
        "book", "before", "listed after", "PRICED after", "latest (min)", "reading",
    ):
        table.add_column(column, justify="right" if column != "book" else "left")
    for book in sorted(set(quotes_before) | set(quotes_after)):
        listed = quotes_after.get(book, 0)
        priced = priced_after.get(book, 0)
        if priced:
            reading = "[green]priced[/green]"
        elif listed:
            reading = "[yellow]listed, unpriced[/yellow]"
        else:
            reading = "[yellow]pre-match only[/yellow]"
        table.add_row(
            book, f"{quotes_before.get(book, 0):,}", f"{listed:,}", f"{priced:,}",
            f"{latest_priced.get(book, 0.0):.0f}" if priced else "-", reading,
        )
    console.print(table)
    console.print(
        "  [dim]The PRICED column is the one that matters, and it is the "
        "number adverse-selection counts. A market listed after first pitch "
        "with suspended or empty outcomes is not a quote.[/dim]"
    )

    # --- the denominator ---------------------------------------------------
    #
    # 2,715 is not "many" and not "near zero". The question is what fraction of
    # the in-play moments the poller actually observed carried a price, and
    # that needs the moments counted, not assumed. Every odds fetch timestamp
    # is in the archive, so the opportunity count is measurable: for each
    # fetch, how many games were live at that instant.
    # The first fifteen minutes past a SCHEDULED start are not in-play: a 19:05
    # that actually throws at 19:12 leaves the pre-game price standing. They are
    # excluded from BOTH sides of the ratio, because counting them in the
    # numerator alone reports full coverage built entirely out of slack.
    slack = timedelta(minutes=15)
    ordered_fetches = sorted(fetch_times)
    opportunities = 0
    live_games: set[Any] = set()
    for stamp in ordered_fetches:
        live = [
            key for key, start in starts.items()
            if start + slack < stamp <= start + timedelta(minutes=game_minutes)
        ]
        opportunities += len(live)
        live_games.update(live)

    cadence = None
    if len(ordered_fetches) > 1:
        deltas = [
            (b - a).total_seconds() / 60.0
            for a, b in zip(ordered_fetches, ordered_fetches[1:], strict=False)
        ]
        cadence = statistics.median(deltas)

    total_priced = sum(past_commence.values())
    slack_priced = sum(past_commence.get(b, 0) for b in ("0-5 min", "5-15 min"))
    priced_reference = total_priced - slack_priced
    console.print(
        f"\n[bold]Denominator[/bold] (game_minutes={game_minutes:.0f}, "
        f"median odds cadence "
        + (f"{cadence:.1f} min" if cadence is not None else "unknown") + ")"
    )
    console.print(f"  games the feed listed:              {len(starts):,}")
    console.print(f"  games observed live past the slack:  {len(live_games):,}")
    console.print(f"  in-play (game, fetch) moments:       {opportunities:,}")
    console.print(
        f"  first-pitch slack (<=15 min):         {slack_priced:,} "
        "[dim](excluded from both sides)[/dim]"
    )
    coverage = priced_reference / opportunities if opportunities else 0.0
    console.print(
        f"  {reference} priced, genuinely in-play: [bold]{priced_reference:,}"
        f"[/bold] = [bold]{coverage * 100:.1f}%[/bold] of moments"
    )

    if past_commence:
        spread = Table(title=f"when {reference}'s in-play prices occur")
        for column in ("minutes past commence", "priced quotes", "share"):
            spread.add_column(
                column, justify="right" if column != "minutes past commence" else "left"
            )
        for band in _MINUTE_BANDS:
            count = past_commence.get(band, 0)
            if not count:
                continue
            spread.add_row(
                band, f"{count:,}", f"{count / total_priced * 100:.1f}%"
            )
        console.print(spread)
        console.print(
            "  [dim]If these cluster in the first few minutes, they are first-"
            "pitch slack -- a scheduled start the game did not keep, and the "
            "pre-game price still standing -- not in-play coverage. Genuine "
            "in-play quoting is spread across the whole window.[/dim]"
        )

    early_share = slack_priced / total_priced if total_priced else 0.0
    console.print("\n[bold]Verdict on the fraction, not on an adjective.[/bold]")
    if coverage >= 0.50:
        console.print(
            f"  {reference} prices [green]most[/green] in-play moments "
            f"({coverage * 100:.1f}%), spread across the game. In-play is "
            "reachable with this feed, and the binding constraint is the "
            "alignment term, not coverage."
        )
    elif coverage >= 0.10:
        console.print(
            f"  [yellow]Partial coverage[/yellow] ({coverage * 100:.1f}%). "
            "Enough to study, not enough to trade on continuously -- a "
            "reference that exists a tenth of the time is not a reference "
            "price, it is an occasional one. Treat in-play as model territory "
            "and re-check as the archive grows."
        )
    else:
        console.print(
            f"  [yellow]Effectively no in-play coverage[/yellow] "
            f"({coverage * 100:.1f}% of in-play moments carry a price"
            + (f"; {early_share * 100:.0f}% of every priced quote past "
               "commence sits inside the first fifteen minutes, which is "
               "first-pitch slack, not in-play quoting" if early_share >= 0.5 else "")
            + "). Pre-game only, for practical purposes. Kalshi's in-play "
            "board is unpriced by this feed and only a model can price it."
        )
    console.print(
        "  [dim]This supersedes the earlier listed-vs-priced verdict, which "
        "was still an adjective on a bare count.[/dim]"
    )

    console.print("\n[bold]Mechanism[/bold], for the record:")
    if not events_after:
        console.print(
            "  The feed drops an event at first pitch entirely."
        )
    elif not sum(priced_after.values()):
        console.print(
            "  Events survive first pitch and h2h markets stay listed, but "
            "none carry prices. A listed-but-suspended market is not a quote."
        )
    else:
        console.print(
            "  Events survive first pitch and some h2h markets carry prices. "
            "How often is what the fraction above answers."
        )
    console.print(
        f"\n[bold]Cross-check:[/bold] the PRICED total here "
        f"({sum(priced_after.values()):,} across all books; "
        f"{priced_after.get('pinnacle', 0):,} for pinnacle) is the same "
        "quantity adverse-selection prints as 'sharp quotes observed AFTER "
        "first pitch'. If the two disagree, one of them is wrong -- do not "
        "reconcile them by picking the more convenient number."
    )
    console.print(
        "\nRecord the answer in reports/odds-feed-pricing.md with today's "
        "date, per the provenance rule."
    )


@app.command("probe-props")
def probe_props(
    market: Annotated[str | None, typer.Option(help="Test only this market key.")] = None,
    region: Annotated[str, typer.Option(help="Single region, to keep the cost to one unit.")] = "us",
    cadence_minutes: Annotated[float, typer.Option(help="Polling cadence for the cost projection.")] = 15.0,
    games_per_day: Annotated[float, typer.Option(help="Slate size for the cost projection.")] = 15.0,
    root: Annotated[Path | None, typer.Option()] = None,
    confirm: Annotated[bool, typer.Option(help="Actually spend the credits.")] = False,
) -> None:
    """Does this plan serve player props, and what does a strikeout line cost?

    Two questions the documentation cannot answer for this account: whether the
    plan includes props at all, and what one call actually bills. Both are in
    the response -- the market keys in the body, the cost in the
    `x-requests-remaining` delta -- so neither is taken on trust.

    The market key is not assumed either. Step one sends a deliberately invalid
    key, because an API that validates the parameter usually enumerates the
    legal values in the error body, and that list is authoritative in a way a
    guess never is. Only then are the configured candidates tried.

    Props bill PER EVENT, not per slate, so the projection at the end is the
    number that decides the pivot: one call for one game, multiplied out to a
    season at your cadence, against the credits actually remaining.

    Costs a handful of credits. Requires --confirm.
    """
    from mlb_edge.http import UpstreamError, client_for

    settings = load_settings(root)
    source = settings.source("odds")
    sport = source.get("sport_key", "baseball_mlb")

    candidates = (
        [market] if market else list(source.get("prop_market_candidates") or [])
    )
    if not candidates:
        console.print(
            "[red]no candidate market keys.[/red] Set "
            "odds.prop_market_candidates in settings.yaml, or pass --market."
        )
        raise typer.Exit(1)

    if not confirm:
        console.print(
            f"Would test {len(candidates) + 1} request shapes against one event "
            f"in region {region!r}: one invalid key to make the API list its "
            f"legal values, then {', '.join(candidates)}.\n"
            "Props bill per event, so the exact cost is unknown until measured "
            "-- that is the point. Re-run with --confirm to spend it."
        )
        return

    # Credentials and the client are only needed once we are actually spending,
    # so the dry run above works on a box that has no key yet.
    client = client_for(settings, "odds")
    key = source.require("api_key")

    def call(url: str, params: dict[str, Any]) -> tuple[int | None, str, Any]:
        """``(remaining, note, parsed_body_or_none)`` for one request."""
        try:
            response = client.get(url, params={"apiKey": key, **params})
        except UpstreamError as exc:
            # The error BODY is the valuable part: an invalid market key or a
            # plan restriction is usually spelled out there, and that text is
            # authoritative where a guess is not.
            return None, f"HTTP {exc.status}: {exc}", None
        headers = {k.lower(): v for k, v in response.headers.items()}
        remaining = None
        with contextlib.suppress(TypeError, ValueError, KeyError):
            remaining = int(headers["x-requests-remaining"])
        used = headers.get("x-requests-used")
        note = f"used={used} remaining={remaining}"
        try:
            return remaining, note, response.json()
        except ValueError:
            return remaining, note, None

    # --- the event list, which props are addressed through -------------------
    events_url = source.endpoint("events", sport=sport)
    remaining, note, events = call(events_url, {})
    console.print(f"\n[bold]events[/bold] {events_url}\n  {note}")
    if not isinstance(events, list) or not events:
        console.print(
            "[red]no event list.[/red] Props are requested per event, so "
            "nothing further can be measured. The note above is the reason."
        )
        raise typer.Exit(1)
    baseline = remaining
    console.print(f"  {len(events)} events listed")

    event = events[0]
    event_id = str(event.get("id"))
    console.print(
        f"  using {event.get('away_team')} @ {event.get('home_team')} "
        f"({event.get('commence_time')})  id={event_id}"
    )
    odds_url = source.endpoint("event_odds", sport=sport, event_id=event_id)

    # --- step one: let the API name its own legal market keys ---------------
    _, invalid_note, _ = call(
        odds_url, {"markets": "__probe_invalid_market__", "regions": region}
    )
    console.print(
        "\n[bold]invalid-key probe[/bold] (asks the API to enumerate what it "
        f"does accept)\n  {invalid_note}"
    )
    console.print(
        "  [dim]Read that line carefully. If it lists market keys, THAT is the "
        "authoritative spelling and the config candidates are guesses to "
        "discard. If it mentions the plan or a subscription, props are not on "
        "this tier.[/dim]"
    )

    # --- step two: the candidates -------------------------------------------
    table = Table(title=f"strikeout prop candidates, one event, region={region}")
    for column in ("market key", "credits", "books quoting", "sample line", "result"):
        table.add_column(column, justify="right" if column == "credits" else "left")

    measured: dict[str, int] = {}
    for candidate in candidates:
        before = baseline
        remaining, note, body = call(
            odds_url, {"markets": candidate, "regions": region}
        )
        cost = (before - remaining) if (before is not None and remaining is not None) else None
        baseline = remaining if remaining is not None else baseline

        books: list[str] = []
        sample = ""
        if isinstance(body, dict):
            for bookmaker in body.get("bookmakers") or []:
                if not isinstance(bookmaker, dict):
                    continue
                for mk in bookmaker.get("markets") or []:
                    if not isinstance(mk, dict) or mk.get("key") != candidate:
                        continue
                    books.append(str(bookmaker.get("key")))
                    outcomes = mk.get("outcomes") or []
                    if outcomes and not sample and isinstance(outcomes[0], dict):
                        first = outcomes[0]
                        sample = (
                            f"{first.get('description') or first.get('name')} "
                            f"{first.get('name')} {first.get('point')} "
                            f"@ {first.get('price')}"
                        )
        if books:
            result = "[green]SERVED[/green]"
            measured[candidate] = cost if cost is not None else 0
        elif body is None:
            result = f"[red]{note}[/red]"
        else:
            result = "[yellow]accepted, no book quoted it[/yellow]"
        table.add_row(
            candidate,
            str(cost) if cost is not None else "?",
            ", ".join(sorted(set(books))[:4]) or "-",
            sample or "-",
            result,
        )
    console.print(table)

    if not measured:
        console.print(
            "\n[bold yellow]No candidate was served.[/bold yellow] Either this "
            "plan does not include player props, or none of the guessed keys "
            "is the right spelling. The invalid-key line above distinguishes "
            "those two, and it is the only place that can: a plan message "
            "means the tier is wrong, a list of keys means the guesses were.\n"
            "Do not buy a tier on the strength of this output alone -- read "
            "that error text first."
        )
        return

    # --- what a season costs ------------------------------------------------
    per_call = max(measured.values())
    console.print(
        f"\n[bold]Cost of props, measured[/bold] ({per_call} credits per event "
        "per market per call)"
    )
    costs = Table()
    for column in ("cadence", "credits/day", "credits/30 days", "vs remaining"):
        costs.add_column(column, justify="right" if column != "cadence" else "left")
    for minutes in sorted({cadence_minutes, 5.0, 15.0, 30.0, 60.0}):
        daily = per_call * games_per_day * (24 * 60 / minutes)
        monthly = daily * 30
        verdict = (
            "[red]over budget[/red]"
            if baseline is not None and monthly > baseline
            else "[green]fits[/green]"
        )
        costs.add_row(
            f"every {minutes:.0f} min", f"{daily:,.0f}", f"{monthly:,.0f}", verdict
        )
    console.print(costs)
    console.print(
        f"  [dim]{games_per_day:.0f} games/day, one market, one region. Props "
        "bill per EVENT, so this scales with the slate as well as the cadence "
        "-- unlike the moneyline, where one call covered the whole board. "
        "Credits remaining now: "
        + (f"{baseline:,}" if baseline is not None else "unknown") + ".[/dim]"
    )
    console.print(
        "\n[bold]Record the measured key and cost[/bold] in "
        "config/settings.yaml, replacing prop_market_candidates with a "
        "`# measured 2026-09-25` comment, and in "
        "reports/props-feasibility.md. Per the provenance rule, a number this "
        "decision rests on does not stay UNVERIFIED."
    )


@app.command("adverse-selection")
def adverse_selection(
    reference: Annotated[str, typer.Option(help="Sharp book to price against.")] = "pinnacle",
    half_spread: Annotated[float, typer.Option(help="Half the Kalshi spread, in probability.")] = 0.01,
    alignment: Annotated[float, typer.Option(help="Timing-error term added to the floor.")] = 0.0,
    min_gaps: Annotated[int, typer.Option(help="Below this, report underpowered and stop.")] = 50,
    series: Annotated[str, typer.Option(help="Kalshi series to price. Only the game-winner series is a moneyline.")] = "KXMLBGAME",
    max_gap: Annotated[float, typer.Option(help="Gaps above this are excluded and counted. 0 disables.")] = MAX_PLAUSIBLE_GAP,
    dump: Annotated[int, typer.Option(help="Print this many individual gap records, end to end.")] = 0,
    dump_min: Annotated[float, typer.Option(help="Only dump gaps at or above this size.")] = 0.10,
    include_in_play: Annotated[bool, typer.Option(help="Keep quotes after first pitch. Off by default.")] = False,
    flush_rows: Annotated[int, typer.Option(help="Rows held before spilling to staging.")] = 50_000,
    memory_budget_mb: Annotated[float, typer.Option(help="Stop cleanly above this RSS. 0 disables.")] = 0.0,
    keep_staging: Annotated[bool, typer.Option(help="Leave the staging files for inspection.")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """When Kalshi disagreed with the sharp price, which way did it then move?

    Pre-registered in reports/adverse-selection.md, before any gap was measured.
    Reads only the archive already on disk -- no network, no cost.

    Memory is bounded by construction, because the box has no swap and the
    first version was OOM-killed after printing two numbers. Quotes are parsed
    in one streaming pass and spilled to staging parquet; the analysis then
    reads back ONE MATCHUP at a time. Peak is set by the flush size and the
    largest single matchup, not by the archive.

    Reports the qualifying-gap COUNT first. Under --min-gaps the honest output
    is "not enough gaps yet", not a verdict read off thirty observations.
    """
    import json as _json
    import shutil
    import tempfile

    import polars as pl

    from mlb_edge.eval.adverse import (
        Quote,
        ScanCounts,
        attach_outcomes,
        breakeven_toward_rate,
        episodes,
        find_gaps,
        floor_for,
        kalshi_mid,
        sharp_fair,
        summarise,
    )
    from mlb_edge.kalshi_tickers import canonical_name, join_tickers, parse_ticker
    from mlb_edge.market.devig import devig_all
    from mlb_edge.market.prices import american_to_prob
    from mlb_edge.poll import PollArchive
    from mlb_edge.timeutil import parse_iso_utc

    settings = load_settings(root)
    poller_config = settings.section("poller")
    archive_root = Path(poller_config.get("archive_dir", "data/poll"))
    archive = PollArchive(
        archive_root if archive_root.is_absolute() else settings.root / archive_root
    )

    # The budget is on GROWTH, not on total resident memory. The interpreter
    # plus polars is already ~150 MB before a single row is read, so a budget
    # set as a fraction of available memory would abort on the first check on a
    # 2 GB box -- a guard that always trips is worse than no guard.
    baseline = _resident_mb()
    available = _available_mb()
    budget = memory_budget_mb or (available * 0.5 if available else 0.0)
    console.print(
        f"memory: {baseline:.0f} MB resident at start, {available:.0f} MB available"
        + (f", stopping after {budget:.0f} MB of growth" if budget else "")
    )

    staging = Path(tempfile.mkdtemp(prefix="mlb-adverse-"))
    spilled = 0

    def spill(rows: list[dict[str, Any]], tag: str) -> None:
        nonlocal spilled
        if not rows:
            return
        pl.DataFrame(rows).write_parquet(staging / f"{tag}-{spilled:05d}.parquet")
        spilled += 1
        rows.clear()

    def grown_mb() -> float:
        return max(_resident_mb() - baseline, 0.0)

    def over_budget() -> bool:
        return bool(budget) and grown_mb() > budget

    try:
        # --- pass one: parse every row to four columns and spill -----------
        games: dict[str, _OddsGame] = {}
        undated = 0
        after_first_pitch = 0
        buffer: list[dict[str, Any]] = []
        files = archive.files("odds")
        for number, path in enumerate(files, 1):
            frame = pl.read_parquet(path, columns=["payload", "error", "fetched_at"])
            for payload, error, at in zip(
                frame["payload"].to_list(), frame["error"].to_list(),
                frame["fetched_at"].to_list(), strict=False,
            ):
                if error or not payload:
                    continue
                try:
                    events = _json.loads(payload)
                except (ValueError, TypeError):
                    continue
                if not isinstance(events, list):
                    continue
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    home, away = event.get("home_team"), event.get("away_team")
                    if not (home and away):
                        continue
                    prices = _reference_prices(event, reference)
                    if prices is None:
                        continue
                    fair = sharp_fair(
                        american_to_prob(prices[0]), american_to_prob(prices[1])
                    )
                    if not fair:
                        continue
                    pair = _pair_key(str(home), str(away), canonical_name)
                    raw = event.get("commence_time")
                    start = None
                    if raw:
                        with contextlib.suppress(ValueError, TypeError):
                            start = parse_iso_utc(str(raw))
                    if start is None:
                        # Without a start time this row cannot be attached to a
                        # game, and the matchup alone is not a game: these two
                        # teams meet three or four times in a week.
                        undated += 1
                        continue
                    key = f"{pair}|{start.isoformat()}"
                    if key not in games:
                        games[key] = _OddsGame(
                            game_pk=len(games), home_team=str(home),
                            away_team=str(away), start_ts=start,
                        )
                    buffer.append({
                        "pair": key, "at": at,
                        "price": fair.get("shin", next(iter(fair.values()))),
                        # The devigged price is the probability of the HOME
                        # team. Recording whose it is turns a comparison that
                        # was wrong by 1-2p into one that refuses when it
                        # cannot tell.
                        "team": canonical_name(str(home)),
                        "home": str(home), "away": str(away),
                        "raw_home": prices[0], "raw_away": prices[1],
                    })
                    if at > start:
                        after_first_pitch += 1
            if len(buffer) >= flush_rows:
                spill(buffer, "sharp")
            if number % 50 == 0 or number == len(files):
                console.print(
                    f"  odds {number:,}/{len(files):,} files   "
                    f"+{grown_mb():.0f} MB", highlight=False
                )
            if over_budget():
                spill(buffer, "sharp")
                console.print(
                    f"[red]stopping: grew {grown_mb():.0f} MB, past the "
                    f"{budget:.0f} MB budget.[/red] Lower --flush-rows, or raise "
                    "--memory-budget-mb if you know the box can take it."
                )
                raise typer.Exit(2)
        spill(buffer, "sharp")

        files = archive.files("kalshi")
        unsided: dict[str, int] = {}
        skipped_series: dict[str, int] = {}
        other_series: dict[str, str] = {}
        unparsed = 0
        event_tickers: set[str] = set()
        kalshi_seen: list[Any] = []
        for number, path in enumerate(files, 1):
            frame = pl.read_parquet(
                path, columns=["endpoint", "key", "payload", "error", "fetched_at"]
            )
            for endpoint, key, payload, error, at in zip(
                frame["endpoint"].to_list(), frame["key"].to_list(),
                frame["payload"].to_list(), frame["error"].to_list(),
                frame["fetched_at"].to_list(), strict=False,
            ):
                if endpoint != "orderbook" or error or not payload or not key:
                    continue
                parsed = parse_ticker(str(key))
                if parsed is None:
                    unparsed += 1
                    other_series.setdefault("(unparseable)", str(key))
                    skipped_series["(unparseable)"] = (
                        skipped_series.get("(unparseable)", 0) + 1
                    )
                    continue
                # The poller collects every MLB series it is configured for --
                # KXMLBGAME, KXMLBWINNER, KXMLBTOTAL. Only the game-winner
                # series is a moneyline, and only a moneyline is comparable to
                # a devigged h2h price. A totals ticker's suffix is its STRIKE
                # (`...-11` is eleven runs), which is why reading it as a side
                # produced codes 5 through 12 rather than team codes.
                #
                # These are a different market, not lost moneyline data, and
                # they are reported as such rather than as a parse failure.
                if parsed.series != series:
                    skipped_series[parsed.series] = (
                        skipped_series.get(parsed.series, 0) + 1
                    )
                    other_series.setdefault(parsed.series, str(key))
                    continue
                pairs = _orderbook_pairs(payload)
                if pairs is None:
                    continue
                mid = kalshi_mid(pairs[0], pairs[1])
                if mid is None:
                    continue
                # Which team does YES pay on? Without this the mid is a number
                # with no referent, and half of them are the complement of what
                # the sharp leg holds.
                side = parsed.side_team
                if side is None:
                    unsided[parsed.side_code or "(none)"] = (
                        unsided.get(parsed.side_code or "(none)", 0) + 1
                    )
                    continue
                event_tickers.add(parsed.event_ticker)
                if len(kalshi_seen) < 1 or at > kalshi_seen[-1]:
                    kalshi_seen.append(at)
                buffer.append({
                    # The event ticker IS the game -- date and start time
                    # included. The team pair is not: it repeats every series.
                    "pair": parsed.event_ticker,
                    "at": at, "price": mid, "team": side,
                    "ticker": str(key),
                    "best_yes": max(price for price, _ in pairs[0]),
                    "best_no": max(price for price, _ in pairs[1]),
                })
            if len(buffer) >= flush_rows:
                spill(buffer, "kalshi")
            if number % 100 == 0 or number == len(files):
                console.print(
                    f"  kalshi {number:,}/{len(files):,} files   "
                    f"+{grown_mb():.0f} MB", highlight=False
                )
            if over_budget():
                spill(buffer, "kalshi")
                console.print(
                    f"[red]stopping: grew {grown_mb():.0f} MB, past the "
                    f"{budget:.0f} MB budget.[/red] Lower --flush-rows."
                )
                raise typer.Exit(2)
        spill(buffer, "kalshi")

        sharp_files = sorted(staging.glob("sharp-*.parquet"))
        kalshi_files = sorted(staging.glob("kalshi-*.parquet"))
        if not sharp_files or not kalshi_files:
            console.print(
                f"[red]nothing to analyse[/red] -- sharp rows: {len(sharp_files)} "
                f"staging files, kalshi: {len(kalshi_files)}. "
                f"Check that {reference!r} appears in the odds payloads."
            )
            raise typer.Exit(1)

        sharp_scan = pl.scan_parquet(sharp_files)
        kalshi_scan = pl.scan_parquet(kalshi_files)

        # Join GAME to GAME. The previous version keyed both sides on the team
        # pair alone, which merged every meeting between two teams in the
        # archive into a single series -- three or four games a week, with one
        # first pitch and one close standing for all of them. join_tickers
        # already separates games, doubleheaders included, and refuses the ones
        # it cannot tell apart.
        join = join_tickers(sorted(event_tickers), list(games.values()))
        by_pk = {game.game_pk: key for key, game in games.items()}
        matched = [
            (by_pk[pk], ticker, games[by_pk[pk]].start_ts)
            for pk, ticker in join.matched.items()
            if pk in by_pk
        ]
        matched.sort()

        console.print(
            f"\ngames: [bold]{len(matched):,}[/bold] joined on both venues "
            f"({len(games):,} priced by {reference}, "
            f"{len(event_tickers):,} on the Kalshi board)"
        )
        if join.clock_offset_minutes is not None:
            console.print(
                f"  [dim]ticker clock offset inferred: "
                f"{join.clock_offset_minutes} minutes from UTC[/dim]"
            )
        if join.ambiguous:
            console.print(
                f"  [yellow]{len(join.ambiguous)} game(s) refused as ambiguous"
                "[/yellow] -- doubleheaders the clock could not separate. "
                "Refused rather than guessed."
            )
        if join.unlisted:
            # A game the board has not listed YET is a different thing from one
            # it never listed. The odds feed publishes days ahead; Kalshi lists
            # closer in. Split on the last Kalshi observation so the two are
            # not read as the same shortfall.
            horizon = max(kalshi_seen) if kalshi_seen else None
            future = sum(
                1 for pk in join.unlisted
                if pk in by_pk and horizon is not None
                and games[by_pk[pk]].start_ts > horizon
            )
            console.print(
                f"  [dim]{len(join.unlisted)} game(s) not on the Kalshi board: "
                f"{future} start after the last Kalshi snapshot in the archive "
                f"(not listed YET), {len(join.unlisted) - future} inside the "
                "archive window (not listed at all).[/dim]"
            )
        console.print(
            f"  [dim]accounting: {len(join.matched):,} joined + "
            f"{len(join.ambiguous):,} ambiguous + {len(join.unlisted):,} "
            f"unlisted + {len(join.unmatched):,} unmatched = "
            f"{len(join.matched) + len(join.ambiguous) + len(join.unlisted) + len(join.unmatched):,}"
            f" of {len(games):,} games priced by {reference}[/dim]"
        )
        if undated:
            console.print(
                f"  [yellow]{undated:,} odds rows had no commence_time[/yellow] "
                "and could not be attached to a game."
            )
        if not matched:
            console.print(
                "[red]no games joined on both venues.[/red] The ticker clock "
                "offset or the team aliases are the place to look."
            )
            raise typer.Exit(1)

        # --- pass two: one matchup at a time -------------------------------
        gaps: list[Any] = []
        counts = ScanCounts()
        per_day: dict[str, dict[str, int]] = {}
        under_floor_sizes: list[tuple[float, float]] = []
        both_sides = 0
        paired = 0
        for number, (pair, event_ticker, start) in enumerate(matched, 1):
            sharp_rows = (
                sharp_scan.filter(pl.col("pair") == pair)
                .select(["at", "price", "team", "home", "away", "raw_home", "raw_away"])
                .collect()
            )
            kalshi_rows = (
                kalshi_scan.filter(pl.col("pair") == event_ticker)
                .select(["at", "price", "team", "ticker", "best_yes", "best_no"])
                .collect()
            )
            sharp_quotes = [
                Quote(row["at"], row["price"], team=row["team"], meta={
                    "home": row["home"], "away": row["away"],
                    "raw_home": row["raw_home"], "raw_away": row["raw_away"],
                })
                for row in sharp_rows.iter_rows(named=True)
            ]
            kalshi_quotes = [
                Quote(row["at"], row["price"], team=row["team"], meta={
                    "ticker": row["ticker"], "best_yes": row["best_yes"],
                    "best_no": row["best_no"], "yes_team": row["team"],
                })
                for row in kalshi_rows.iter_rows(named=True)
            ]
            # Kalshi lists ONE MARKET PER SIDE, and both resolve to the same
            # event ticker. Keeping both counts every moment twice, and the
            # two counts are near-complements of each other rather than
            # independent observations -- an n twice the real one, with a
            # confidence interval built on it. Keep one market per game,
            # preferring the side the sharp leg is already stated in so no
            # arithmetic is needed to line them up.
            by_side: dict[str, list[Any]] = {}
            for quote in kalshi_quotes:
                by_side.setdefault(str(quote.team), []).append(quote)
            if len(by_side) > 1:
                both_sides += 1
                sharp_team = sharp_quotes[0].team if sharp_quotes else None
                kalshi_quotes = by_side.get(str(sharp_team)) or max(
                    by_side.values(), key=len
                )

            paired += len(kalshi_quotes)
            local = ScanCounts()
            found = find_gaps(
                kalshi_quotes, sharp_quotes, game_pk=number, first_pitch=start,
                half_spread=half_spread, alignment=alignment,
                max_gap=max_gap or None, pregame_only=not include_in_play,
                counts=local,
            )
            attach_outcomes(found, kalshi_quotes, first_pitch=start)
            gaps.extend(found)
            day = start.date().isoformat()
            tally = per_day.setdefault(day, {"games": 0, "stale": 0, "usable": 0, "gaps": 0})
            tally["games"] += 1
            tally["stale"] += local.stale
            tally["usable"] += local.under_floor + local.implausible + len(found)
            tally["gaps"] += len(found)
            for field_ in ("in_play", "unoriented", "stale", "implausible", "under_floor"):
                setattr(counts, field_, getattr(counts, field_) + getattr(local, field_))
            under_floor_sizes.extend(local.near_misses)
            del sharp_quotes, kalshi_quotes, sharp_rows, kalshi_rows
            if number % 20 == 0 or number == len(matched):
                console.print(
                    f"  game {number:,}/{len(matched):,}   "
                    f"{paired:,} quotes paired   {len(gaps):,} gaps   "
                    f"+{grown_mb():.0f} MB", highlight=False
                )
            if over_budget():
                console.print(
                    f"[red]stopping at matchup {number}: grew {grown_mb():.0f} MB, "
                    f"past the {budget:.0f} MB budget.[/red] This matchup has more "
                    "quotes than the budget allows; raise it or narrow the archive."
                )
                raise typer.Exit(2)
    finally:
        if keep_staging:
            console.print(f"[dim]staging kept at {staging}[/dim]")
        else:
            shutil.rmtree(staging, ignore_errors=True)

    if skipped_series:
        table = Table(title="orderbook rows skipped: not the game-winner market")
        for column in ("series", "rows", "example ticker", "what it is"):
            table.add_column(column, justify="right" if column == "rows" else "left")
        meaning = {
            "KXMLBTOTAL": "total runs; suffix is the strike, not a team",
            "KXMLBWINNER": "season/series winner, not a single game",
            "(unparseable)": "ticker did not match the game format",
        }
        for name, count in sorted(skipped_series.items(), key=lambda kv: -kv[1]):
            table.add_row(
                name, f"{count:,}", other_series.get(name, ""),
                meaning.get(name, "a different market type"),
            )
        console.print(table)
        console.print(
            f"  [dim]These are not lost moneyline data. Only {series} is a "
            "moneyline, and only a moneyline is comparable to a devigged h2h "
            "price -- tier 0 buys no totals reference to price the others "
            "against. Excluding them does not shrink the moneyline sample.[/dim]\n"
        )

    if unsided:
        total = sum(unsided.values())
        console.print(
            f"[yellow]dropped {total:,} {series} quotes whose side could not "
            f"be resolved[/yellow] -- codes: "
            + ", ".join(f"{code}={n:,}" for code, n in sorted(
                unsided.items(), key=lambda kv: -kv[1])[:8])
        )
        console.print(
            "  A quote with no resolvable side has no referent. Comparing it "
            "anyway is wrong by 1-2p on half of them.\n"
        )

    if both_sides:
        console.print(
            f"[dim]{both_sides:,} game(s) had a market on both sides; one was "
            "used. Two sides of the same game are near-complements, not two "
            "observations.[/dim]\n"
        )

    total_sharp = after_first_pitch
    console.print(
        f"sharp quotes observed AFTER first pitch: [bold]{total_sharp:,}[/bold]"
    )
    console.print(
        "  [dim]A COUNT, with no verdict attached. 2,715 is not 'many' or "
        "'near zero' -- those are adjectives, and the same number carried "
        "both in two outputs of this repo. What decides it is the fraction of "
        "in-play moments that carried a price, which needs a denominator this "
        "command does not build. Run `mlb-edge probe-inplay`, which does, and "
        "read its verdict. This number must equal the PRICED total there.[/dim]\n"
    )

    console.print("quotes excluded, by reason:")
    console.print(f"  {counts.line()}")
    if counts.in_play:
        console.print(
            f"  [dim]in-play: the sharp feed is pre-match, so its last quote "
            f"stands frozen at first pitch while Kalshi keeps trading. Those "
            f"{counts.in_play:,} comparisons measure the game, not a gap.[/dim]"
        )
    if counts.implausible:
        console.print(
            f"  [red]{counts.implausible:,} gaps exceeded the "
            f"{(max_gap or 0) * 100:.0f}pp plausibility bound and were "
            f"EXCLUDED.[/red] Two venues pricing the same game do not disagree "
            "by this much. Treat a large count here as a bug report, not a "
            "finding -- re-run with --dump to see the records."
        )
    console.print()

    if dump and gaps:
        _dump_gaps(gaps, dump, dump_min, devig_all, american_to_prob)

    console.print(f"qualifying gaps above the floor: [bold]{len(gaps):,}[/bold]")
    distinct = len({g.game_pk for g in gaps})
    runs = episodes(gaps)
    console.print(
        f"  in [bold]{distinct:,}[/bold] distinct games, as "
        f"[bold]{runs:,}[/bold] separate episodes "
        f"({len(gaps) / runs:.1f} snapshots per episode)"
        if runs else "  no episodes"
    )
    console.print(
        "  [dim]The EPISODE count is the unit of independent information. A "
        "dislocation that lasts ninety minutes is six rows at a 15-minute "
        "cadence and forty-five at two minutes, so the gap count scales with "
        "polling frequency while the information does not. --min-gaps is a "
        "row threshold and can be met by polling faster; that is not more "
        "evidence.[/dim]\n"
    )

    if per_day:
        days = Table(title="by game date: is the rate constant, or did it change?")
        for column in ("date", "games", "usable quotes", "stale", "stale %", "gaps"):
            days.add_column(column, justify="right" if column != "date" else "left")
        for day in sorted(per_day):
            row = per_day[day]
            total = row["usable"] + row["stale"]
            days.add_row(
                day, f"{row['games']:,}", f"{row['usable']:,}", f"{row['stale']:,}",
                f"{row['stale'] / total * 100:.0f}%" if total else "-",
                f"{row['gaps']:,}",
            )
        console.print(days)
        console.print(
            "  [dim]STALE means the most recent SHARP quote was older than the "
            "staleness limit -- it is set by the ODDS cadence, not the Kalshi "
            "one. If the stale column falls sharply partway down this table, "
            "the early archive was collected at a slower odds cadence and the "
            "headline rate understates the forward rate. Read the last few "
            "days, not the average.[/dim]\n"
        )

    if under_floor_sizes:
        bands = Table(title="under-floor near misses: would a cheaper fee have helped?")
        for column in ("band", "n", "mean price", "mean floor", "would qualify at P=0.80"):
            bands.add_column(column, justify="right" if column != "band" else "left")
        for low, high in ((0.010, 0.015), (0.015, 0.020), (0.020, 0.0275)):
            inside = [(d, p) for d, p in under_floor_sizes if low <= d < high]
            if not inside:
                continue
            tail_floor = floor_for(0.80, half_spread=half_spread, alignment=alignment)
            would = sum(1 for d, _ in inside if d > tail_floor)
            bands.add_row(
                f"{low * 100:.1f}-{high * 100:.2f}pp", f"{len(inside):,}",
                f"{statistics.fmean([p for _, p in inside]):.3f}",
                f"{statistics.fmean([floor_for(p, half_spread=half_spread, alignment=alignment) for _, p in inside]) * 100:.2f}pp",
                f"{would:,}",
            )
        console.print(bands)
        console.print(
            "  [dim]The fee is 0.07*P*(1-P), maximal at a coin flip. MLB "
            "moneylines live between about 0.35 and 0.65, where the fee is "
            "within 0.2pp of its maximum, so tail pricing buys almost no "
            "relief: even a P=0.80 favourite only lowers the floor by 0.63pp, "
            "and baseball rarely goes past that.[/dim]\n"
        )

    if len(gaps) < min_gaps:
        console.print(
            f"[yellow]NOT ENOUGH GAPS YET.[/yellow] {len(gaps)} qualifying gaps is "
            f"below the pre-registered minimum of {min_gaps}.\n"
            "No convergence figure is reported, because one read off this many "
            "observations would not survive its own confidence interval. "
            "Keep collecting and re-run."
        )
        return

    _report_convergence(gaps, summarise, breakeven_toward_rate)


#: Where an in-play price sits relative to the scheduled start. The first two
#: bands are first-pitch slack, not in-play coverage: a scheduled 19:05 that
#: actually throws at 19:12 leaves the pre-game price standing for minutes.
_MINUTE_BANDS = (
    "0-5 min", "5-15 min", "15-30 min", "30-60 min",
    "60-120 min", "120-180 min", "180+ min",
)


def _minute_band(minutes: float) -> str:
    for edge, name in zip((5, 15, 30, 60, 120, 180), _MINUTE_BANDS, strict=False):
        if minutes <= edge:
            return name
    return _MINUTE_BANDS[-1]


def _dump_gaps(
    gaps: list[Any], limit: int, floor_size: float, devig_all: Any, american_to_prob: Any
) -> None:
    """Print individual gap records end to end.

    Every number that went into one comparison, in the order it was derived, so
    a wrong answer can be traced to the step that produced it rather than
    inferred from an aggregate. Aggregates are how a side inversion survived to
    a verdict: 300,000 of them averaged to a number that looked like an edge.
    """
    large = sorted(
        (g for g in gaps if g.size >= floor_size), key=lambda g: -g.size
    )[:limit]
    if not large:
        console.print(
            f"[dim]no gaps at or above {floor_size * 100:.0f}pp to dump.[/dim]\n"
        )
        return

    console.print(
        f"[bold]{len(large)} largest gaps at or above {floor_size * 100:.0f}pp"
        f"[/bold] (of {sum(1 for g in gaps if g.size >= floor_size):,})\n"
    )
    for index, gap in enumerate(large, 1):
        meta = gap.meta
        console.print(f"[bold]--- {index} ---[/bold]")
        console.print(f"  at                {gap.at:%Y-%m-%d %H:%M:%S} UTC"
                      f"   ({gap.minutes_to_first_pitch:+.0f} min to first pitch)")
        console.print(f"  matchup           {meta.get('away')} @ {meta.get('home')}")
        console.print(f"  ticker            {meta.get('ticker')}")
        console.print(f"  ticker YES side   {meta.get('yes_team')}")
        console.print(f"  best yes bid      {meta.get('best_yes')}")
        console.print(f"  best no bid       {meta.get('best_no')}"
                      f"   (= yes ask {1.0 - float(meta.get('best_no') or 0):.4f})")
        console.print(f"  kalshi mid (YES)  {meta.get('kalshi_as_quoted'):.4f}"
                      f"   probability of {meta.get('kalshi_team')}")
        console.print(f"  sharp raw         {meta.get('home')} {meta.get('raw_home')}"
                      f"  /  {meta.get('away')} {meta.get('raw_away')}")
        try:
            probs = devig_all([
                american_to_prob(meta["raw_home"]), american_to_prob(meta["raw_away"])
            ])
            console.print("  devigged (home)   " + "  ".join(
                f"{name}={values[0]:.4f}" for name, values in probs.items()
            ))
        except Exception as error:  # noqa: BLE001 - provenance, not control flow
            console.print(f"  devigged          [red]unavailable: {error}[/red]")
        console.print(f"  compared as       P({meta.get('team')}): "
                      f"kalshi {gap.kalshi:.4f} vs sharp {gap.sharp:.4f}")
        console.print(f"  gap               [bold]{gap.size * 100:.2f}pp[/bold]"
                      f"   floor {gap.floor * 100:.2f}pp")
        close = gap.later.get("close")
        console.print("  close             "
                      + (f"{close:.4f}   convergence "
                         f"{(gap.convergence('close') or 0) * 100:+.2f}pp"
                         if close is not None else "[dim]none (see exclusions)[/dim]"))
        console.print()


@dataclass(frozen=True)
class _OddsGame:
    """One game as the odds feed describes it, shaped for ``join_tickers``.

    ``game_pk`` here is a local index, not a StatsAPI id -- the joiner only
    needs something hashable to key its result on. The real identity is
    ``(teams, start_ts)``.
    """

    game_pk: int
    home_team: str
    away_team: str
    start_ts: Any


def _pair_key(home: str, away: str, canonical: Any) -> str:
    """A stable string key for an unordered matchup.

    NOT a game key. The same two teams meet three or four times in a series.
    Use it to group a matchup; never to join two venues game by game.
    """
    return "|".join(sorted({canonical(home), canonical(away)}))


def _pp(value: float) -> str:
    """Probability points, with negative zero printed as zero."""
    return f"{(0.0 if value == 0 else value) * 100:+.2f}pp"


def _report_convergence(gaps: list[Any], summarise: Any, breakeven: Any) -> None:
    table = Table(title="realised convergence toward the sharp price")
    for column in ("horizon", "n", "mean", "median", "toward", "floor", "verdict"):
        table.add_column(column, justify="right" if column != "horizon" else "left")

    gating = None
    for horizon in ("+1 snapshot", "+1 hour", "close"):
        result = summarise(gaps, horizon)
        if horizon == "close":
            gating = result
        if result.observations == 0:
            table.add_row(horizon, "0", "-", "-", "-", "-", "[dim]no data[/dim]")
            continue
        if result.negative:
            verdict = "[red]NEGATIVE -- we are the slow side[/red]"
        elif result.clears_floor:
            verdict = "[green]clears the floor[/green]"
        elif result.mean <= 0.0:
            # Exactly zero is not "positive but small". Calling it positive is
            # the same defect as a warning that misdescribes what it found.
            verdict = "[yellow]flat -- no movement either way[/yellow]"
        else:
            verdict = "[yellow]positive but under the floor[/yellow]"
        table.add_row(
            horizon, f"{result.observations:,}",
            f"{_pp(result.mean)}", f"{_pp(result.median)}",
            f"{result.toward_rate * 100:.1f}%", f"{result.floor * 100:.2f}pp", verdict,
        )
    console.print(table)

    buckets = Table(title="toward-rate against its break-even, by gap size")
    for column in (
        "gap", "n", "toward", "b/e", "no move", "sp@gap", "sp@close", "floor",
    ):
        buckets.add_column(column, justify="right" if column != "gap" else "left")
    # The top bucket stops at the plausibility bound, because nothing above it
    # is in `gaps` any more. Labelling it 10-100pp would advertise a range the
    # scan no longer admits.
    top = MAX_PLAUSIBLE_GAP
    for low, high in ((0.0, 0.04), (0.04, 0.06), (0.06, 0.10), (0.10, top)):
        inside = [
            g for g in gaps
            if low <= g.size < high and g.convergence("close") is not None
        ]
        if not inside:
            continue
        toward = sum(1 for g in inside if (g.convergence("close") or 0) > 0)
        # Exactly zero movement counts as NOT toward. On a thin book that is
        # the commonest outcome at small gaps, and it drags the small-gap rate
        # below the 50% a coin flip would give -- which is most of what makes
        # the rate climb with gap size. Reported so the climb is not read as
        # an edge that grows with size.
        still = sum(1 for g in inside if (g.convergence("close") or 0) == 0.0)
        def spread_of(gap: Any, prefix: str = "") -> float | None:
            yes = gap.meta.get(f"{prefix}best_yes")
            no = gap.meta.get(f"{prefix}best_no")
            if yes is None or no is None:
                return None
            return (1.0 - float(no)) - float(yes)

        spreads = [s for g in inside if (s := spread_of(g)) is not None]
        at_close = [s for g in inside if (s := spread_of(g, "close_")) is not None]
        need = breakeven(
            statistics.fmean([g.size for g in inside]),
            statistics.fmean([g.floor for g in inside]),
        )
        buckets.add_row(
            f"{low * 100:.0f}-{high * 100:.0f}pp", f"{len(inside):,}",
            f"{toward / len(inside) * 100:.1f}%", f"{need * 100:.1f}%",
            f"{still / len(inside) * 100:.1f}%",
            f"{statistics.fmean(spreads) * 100:.2f}pp" if spreads else "-",
            f"{statistics.fmean(at_close) * 100:.2f}pp" if at_close else "-",
            f"{statistics.fmean([g.floor for g in inside]) * 100:.2f}pp",
        )
    console.print(buckets)
    console.print(
        "  [dim]'no move' is the share whose close was IDENTICAL to the price "
        "at the gap; those count as not-toward. The two spread columns are the "
        "test that matters: if spread@gap is much WIDER than spread@close, "
        "the gap was noise in a mid nobody could trade on, and the book "
        "tightening toward first pitch will read as convergence at any gap "
        "size. A toward-rate near 100% is that pattern, not an edge. Either "
        "spread exceeding twice --half-spread also means the floor beside it "
        "is understated.[/dim]"
    )

    if gating is None or gating.observations == 0:
        console.print(
            "\n[yellow]No gap had a later Kalshi quote to compare against.[/yellow] "
            "Gaps were found but none could be followed to a close. No verdict."
        )
        return
    if gating.negative:
        console.print(
            "\n[bold red]HARD STOP.[/bold red] Mean convergence to Kalshi's close "
            "is negative: the archive says we are the stale side. The "
            "prediction-market-versus-sharp-book trade is dead and stage one "
            "does not run."
        )
        raise typer.Exit(1)
    if not gating.clears_floor:
        if gating.mean <= 0.0:
            # Exactly zero is flat, not positive. Same defect as the table
            # verdict, which was fixed while this line was not.
            console.print(
                "\n[yellow]Convergence is FLAT.[/yellow] Kalshi's close sits "
                "where it was when the gap opened, on average. No signal in "
                "either direction, and nothing to pay the fee and the spread "
                "with."
            )
        else:
            console.print(
                "\n[yellow]Convergence is positive but does not clear the "
                "floor.[/yellow] The signal points the right way and does not "
                "pay for the fee and the spread. Not a hard stop; not a "
                "business yet either."
            )


def _reference_prices(event: dict[str, Any], book_key: str) -> tuple[float, float] | None:
    """``(home, away)`` American prices from one book's h2h market."""
    home, away = event.get("home_team"), event.get("away_team")
    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict) or bookmaker.get("key") != book_key:
            continue
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict) or market.get("key") != "h2h":
                continue
            prices: dict[str, float] = {}
            for outcome in market.get("outcomes") or []:
                if isinstance(outcome, dict) and outcome.get("name") and outcome.get("price"):
                    prices[str(outcome["name"])] = float(outcome["price"])
            if home in prices and away in prices:
                return prices[str(home)], prices[str(away)]
    return None


def _orderbook_pairs(payload: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """``(yes, no)`` price/size levels, for the mid calculation."""
    import json as _json

    try:
        data = _json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for container in (*ORDERBOOK_CONTAINERS, None):
        node = data.get(container) if container else data
        if not isinstance(node, dict):
            continue
        sides: dict[str, list[tuple[float, float]]] = {"yes": [], "no": []}
        seen = False
        for key, value in node.items():
            side = _side_of(key)
            if side is None or not isinstance(value, list):
                continue
            seen = True
            for entry in value:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    try:
                        sides[side].append((float(entry[0]), float(entry[1])))
                    except (TypeError, ValueError):
                        continue
        if seen:
            return sides["yes"], sides["no"]
    return None


@app.command(name="poll-sources")
def poll_sources(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Which venues the poller would actually poll, and why the others are out.

    Deploy runs this to check it achieved something. A deploy that prints green
    and leaves a service restarting on an empty source list is the same failure
    shape as a poller reporting `captured 0/0`.
    """
    settings = load_settings(root)
    any_enabled = False
    for venue in ("odds", "kalshi", "polymarket"):
        source = settings.source(venue)
        if not source.enabled:
            console.print(f"{venue:<12} [dim]disabled[/dim] (sources.{venue}.enabled)")
            continue
        missing = [
            key for key in source.raw if source.raw[key] == MISSING_SECRET
        ]
        if missing:
            console.print(
                f"{venue:<12} [yellow]enabled but unusable[/yellow]: "
                f"{', '.join(missing)} unset in the environment"
            )
            continue
        any_enabled = True
        console.print(f"{venue:<12} [green]enabled: will be polled[/green]")

    if not any_enabled:
        console.print(
            "\n[red]nothing to poll.[/red] Set the flags in config/local.yaml "
            "(git-ignored, survives deploy) and the keys in the environment file."
        )
        raise typer.Exit(1)


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
