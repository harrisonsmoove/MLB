"""Off-box backup, and a restore that has actually been run.

What is irreplaceable, in order:

1. ``data/poll/`` -- the raw archive. Tier 0 has no historical endpoint, so a
   lost hour is lost permanently. Nothing can rebuild it.
2. ``data/raw/`` -- cached upstream payloads. Re-fetchable, but a full Statcast
   backfill is hours of rate-limited requests.
3. ``data/warehouse/`` -- derived. Rebuildable from 1 and 2, and the slowest
   thing to lose but the least serious.

The warehouse is captured with DuckDB's ``EXPORT DATABASE`` rather than by
copying the file. A file copy is only readable by a compatible DuckDB build,
which is a poor property for something whose entire purpose is to be read after
something went wrong; the export is parquet plus SQL and will outlive the
version that wrote it. It also avoids copying a file another process may be
mid-write on.

Everything is checksummed on the way out and verified on the way back in. A
backup that has never been restored is a hypothesis.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow

MANIFEST_NAME = "MANIFEST.json"
WAREHOUSE_EXPORT_DIR = "warehouse-export"


@dataclass
class BackupEntry:
    path: str
    sha256: str
    bytes: int


@dataclass
class BackupManifest:
    created_at: str
    root: str
    entries: list[BackupEntry] = field(default_factory=list)
    warehouse_tables: dict[str, int] = field(default_factory=dict)
    notes: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(e.bytes for e in self.entries)

    def summary(self) -> str:
        return (
            f"{len(self.entries):,} files, {self.total_bytes / 1e6:,.1f} MB, "
            f"{len(self.warehouse_tables)} warehouse tables"
        )


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _link_or_copy(source: Path, target: Path) -> None:
    """Hardlink where the filesystem allows it, copy otherwise.

    The poll archive is immutable once written, so a hardlink is a correct and
    almost free snapshot. Falls back to a copy across filesystems.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def create(
    *,
    root: Path,
    destination: Path,
    warehouse_path: Path | None = None,
    include: tuple[str, ...] = ("data/poll", "data/raw"),
    now: datetime | None = None,
) -> BackupManifest:
    """Snapshot the irreplaceable state into ``destination``."""
    root = Path(root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    manifest = BackupManifest(
        created_at=(now or utcnow()).isoformat(), root=str(root.resolve())
    )

    for relative in include:
        source_dir = root / relative
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.rglob("*")):
            if not source.is_file():
                continue
            rel = source.relative_to(root)
            _link_or_copy(source, destination / rel)
            manifest.entries.append(
                BackupEntry(
                    path=str(rel), sha256=_sha256(source), bytes=source.stat().st_size
                )
            )

    if warehouse_path and Path(warehouse_path).is_file():
        manifest.warehouse_tables = _export_warehouse(
            Path(warehouse_path), destination / WAREHOUSE_EXPORT_DIR
        )
        export_dir = destination / WAREHOUSE_EXPORT_DIR
        for source in sorted(export_dir.rglob("*")):
            if source.is_file():
                manifest.entries.append(
                    BackupEntry(
                        path=str(source.relative_to(destination)),
                        sha256=_sha256(source),
                        bytes=source.stat().st_size,
                    )
                )

    (destination / MANIFEST_NAME).write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True), "utf-8"
    )
    return manifest


def _export_warehouse(warehouse_path: Path, export_dir: Path) -> dict[str, int]:
    """``EXPORT DATABASE`` to parquet, plus the row counts to check against."""
    import duckdb

    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        counts: dict[str, int] = {}
        for (name,) in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall():
            counts[name] = int(
                connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            )
        connection.execute(
            f"EXPORT DATABASE '{export_dir}' (FORMAT PARQUET)"
        )
        return counts
    finally:
        connection.close()


def read_manifest(backup_dir: Path) -> BackupManifest:
    payload = json.loads((Path(backup_dir) / MANIFEST_NAME).read_text("utf-8"))
    entries = [BackupEntry(**e) for e in payload.get("entries", [])]
    return BackupManifest(
        created_at=payload["created_at"],
        root=payload["root"],
        entries=entries,
        warehouse_tables=payload.get("warehouse_tables", {}),
        notes=payload.get("notes", ""),
    )


def verify(backup_dir: Path) -> tuple[bool, list[str]]:
    """Recompute every checksum. Returns ``(ok, problems)``."""
    backup_dir = Path(backup_dir)
    manifest = read_manifest(backup_dir)
    problems: list[str] = []

    for entry in manifest.entries:
        path = backup_dir / entry.path
        if not path.is_file():
            problems.append(f"missing: {entry.path}")
            continue
        if path.stat().st_size != entry.bytes:
            problems.append(f"size changed: {entry.path}")
            continue
        if _sha256(path) != entry.sha256:
            problems.append(f"checksum mismatch: {entry.path}")

    return not problems, problems


def restore(
    *,
    backup_dir: Path,
    into_root: Path,
    warehouse_path: Path | None = None,
    verify_first: bool = True,
) -> tuple[BackupManifest, list[str]]:
    """Restore a backup into ``into_root``. Returns ``(manifest, problems)``.

    Refuses to restore an archive that fails its own checksums, unless
    explicitly told not to check -- restoring corruption over good data is a
    worse outcome than a failed restore.
    """
    backup_dir, into_root = Path(backup_dir), Path(into_root)
    problems: list[str] = []

    if verify_first:
        ok, problems = verify(backup_dir)
        if not ok:
            return read_manifest(backup_dir), problems

    manifest = read_manifest(backup_dir)
    for entry in manifest.entries:
        if entry.path.startswith(WAREHOUSE_EXPORT_DIR):
            continue
        source = backup_dir / entry.path
        target = into_root / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    export_dir = backup_dir / WAREHOUSE_EXPORT_DIR
    if warehouse_path and export_dir.is_dir():
        problems.extend(
            _import_warehouse(export_dir, Path(warehouse_path), manifest.warehouse_tables)
        )
    return manifest, problems


def _import_warehouse(
    export_dir: Path, warehouse_path: Path, expected: dict[str, int]
) -> list[str]:
    """``IMPORT DATABASE`` and check the row counts came back."""
    import duckdb

    warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    if warehouse_path.exists():
        warehouse_path.unlink()

    problems: list[str] = []
    connection = duckdb.connect(str(warehouse_path))
    try:
        connection.execute(f"IMPORT DATABASE '{export_dir}'")
        for table, count in sorted(expected.items()):
            try:
                actual = int(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{table}: not restored ({exc})")
                continue
            if actual != count:
                problems.append(f"{table}: {actual:,} rows restored, expected {count:,}")
    finally:
        connection.close()
    return problems


def push(
    *,
    backup_dir: Path,
    command: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Run the configured upload command with ``{src}`` substituted.

    Shelling out rather than embedding an S3 client: the tool that already works
    on the box (``aws s3 sync``, ``rclone``, ``rsync``) is better tested than
    anything added here, and credentials stay in its own configuration rather
    than being handled by this process.
    """
    rendered = command.format(src=str(Path(backup_dir).resolve()))
    if dry_run:
        return True, rendered
    try:
        result = subprocess.run(
            rendered, shell=True, capture_output=True, text=True, timeout=3600
        )
    except subprocess.TimeoutExpired:
        return False, f"upload timed out after 1h: {rendered}"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()[-4000:]


def prune(backup_root: Path, *, keep: int) -> tuple[list[Path], list[str]]:
    """Delete all but the newest ``keep`` dated backup directories.

    Returns ``(removed, problems)`` and never raises. Failing to delete an old
    copy is not a failure to back up, and it must not be able to say otherwise.

    Observed live: a directory created while the job still ran as root, then a
    switch to the service user, and ``shutil.rmtree`` raising PermissionError on
    MANIFEST.json -- *after* the push had already succeeded. The backup was
    safely off-box and the command exited non-zero, so the systemd timer read
    red on a run that worked.

    That is this project's usual bug reflected: normally the exit code is green
    when something failed, here it was red when everything succeeded. Both
    destroy the same thing, which is the exit code meaning what it says. A timer
    that cries wolf gets muted exactly like an alert that does.
    """
    backup_root = Path(backup_root)
    if not backup_root.is_dir():
        return [], []
    dated = sorted(
        (p for p in backup_root.iterdir() if p.is_dir() and (p / MANIFEST_NAME).is_file()),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    problems: list[str] = []
    for path in dated[: max(len(dated) - keep, 0)]:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        removed.append(path)
    return removed, problems


# ---------------------------------------------------------------------------
# Is there actually a copy off this box?
# ---------------------------------------------------------------------------
@dataclass
class BackupState:
    """When a backup last succeeded, and when one last left the box.

    Two timestamps, not one, because they fail independently and only the
    second one matters. ``push_command`` was empty for 23 days: every backup
    "succeeded", the systemd timer stayed green, and the only signal was a
    yellow line at deploy time that scrolled past. The archive was one droplet
    failure from gone while everything reported healthy.

    That is this project's recurring bug in its purest form -- a degraded path
    reporting success -- so the off-box copy gets the same treatment as the
    poller's heartbeat: a timestamp, a threshold, and an alert when it lapses.
    """

    path: Path
    last_backup_at: str | None = None
    last_push_at: str | None = None
    last_push_error: str | None = None
    push_configured: bool = False

    @classmethod
    def load(cls, path: Path) -> BackupState:
        path = Path(path)
        state = cls(path=path)
        if not path.is_file():
            return state
        try:
            raw = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - corrupt state must not stop a backup
            print(f"[backup] WARN: could not read {path}; starting fresh", flush=True)
            return state
        state.last_backup_at = raw.get("last_backup_at")
        state.last_push_at = raw.get("last_push_at")
        state.last_push_error = raw.get("last_push_error")
        state.push_configured = bool(raw.get("push_configured", False))
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_backup_at": self.last_backup_at,
            "last_push_at": self.last_push_at,
            "last_push_error": self.last_push_error,
            "push_configured": self.push_configured,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
        temporary.replace(self.path)

    def record_backup(self, *, now: datetime) -> None:
        self.last_backup_at = now.isoformat()

    def record_push(self, *, now: datetime, ok: bool, error: str | None = None) -> None:
        self.push_configured = True
        if ok:
            self.last_push_at = now.isoformat()
            self.last_push_error = None
        else:
            self.last_push_error = (error or "")[:500]

    def off_box_age(self, now: datetime) -> timedelta | None:
        """How long since a copy last left the box. ``None`` means never."""
        if not self.last_push_at:
            return None
        return ensure_utc(now) - parse_iso_utc(self.last_push_at)

