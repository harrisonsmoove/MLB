"""The gate on ``model/``.

Nothing gets written under ``src/mlb_edge/model/`` until both of these hold, in
this order:

1. ``mlb-edge verify`` runs clean -- zero ERRORs.
2. The fitted strikeout regression constant passes the pre-registered ratio
   check in :mod:`mlb_edge.features.preregistration`.

The order matters and the gate enforces it. A pre-registered check computed on a
warehouse that fails its integrity checks is not evidence of anything: if
doubleheaders are mis-keyed or closing lines leaked into features, the fitted
constant is a number derived from corrupted input, and it passing would be
worse than it failing because it would look like permission to proceed.

A passing run writes ``reports/gate.json``. ``tests/test_model_gate.py`` refuses
any module under ``model/`` unless that record exists and says the gate passed,
so the rule is enforced by the test suite rather than by remembering it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mlb_edge.features.preregistration import (
    GATING_PLAYER_TYPE,
    check_constants,
    gating_result,
    interpret,
)
from mlb_edge.storage import schema
from mlb_edge.storage.schema import TableKind
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import utcnow

GATE_FILENAME = "gate.json"


def warehouse_fingerprint(wh: Warehouse) -> dict[str, Any]:
    """A fingerprint of the data the gate's verdict was computed from.

    Row count and latest as-of timestamp per table, hashed. Both matter: rows
    alone would miss a Statcast restatement that revises values without adding
    rows, and as-of alone would miss a deletion.

    Without this a single clean run unlocks ``model/`` permanently, including
    after the warehouse has been rebuilt, extended by five seasons, or had a
    parser fix change what is in it. The verdict would still be sitting there
    saying yes, about data that no longer exists.
    """
    tables: dict[str, dict[str, Any]] = {}
    for spec in schema.TABLES:
        if spec.kind == TableKind.META:
            continue
        rows = wh.count(spec.name)
        latest = wh.max_as_of(spec.name) if spec.as_of_column else None
        tables[spec.name] = {
            "rows": rows,
            "max_as_of": latest.isoformat() if latest else None,
        }

    payload = json.dumps(tables, sort_keys=True).encode()
    return {"digest": hashlib.sha256(payload).hexdigest(), "tables": tables}


def fingerprint_diff(
    recorded: dict[str, Any] | None, current: dict[str, Any]
) -> list[str]:
    """Human-readable description of what moved since the record was written."""
    if not recorded:
        return ["the record carries no fingerprint (written before expiry existed)"]
    if recorded.get("digest") == current.get("digest"):
        return []

    old_tables = recorded.get("tables", {}) or {}
    new_tables = current.get("tables", {}) or {}
    changes: list[str] = []
    for name in sorted(set(old_tables) | set(new_tables)):
        before, after = old_tables.get(name), new_tables.get(name)
        if before == after:
            continue
        if before is None:
            changes.append(f"{name}: new table ({after['rows']:,} rows)")
        elif after is None:
            changes.append(f"{name}: table gone (was {before['rows']:,} rows)")
        else:
            if before["rows"] != after["rows"]:
                delta = after["rows"] - before["rows"]
                changes.append(
                    f"{name}: {before['rows']:,} -> {after['rows']:,} rows ({delta:+,})"
                )
            if before["max_as_of"] != after["max_as_of"]:
                changes.append(
                    f"{name}: latest as_of {before['max_as_of']} -> {after['max_as_of']}"
                )
    return changes or ["fingerprint differs but no per-table change was identified"]


@dataclass
class GateResult:
    passed: bool = False
    evaluated_at: str = ""
    integrity_passed: bool = False
    integrity_errors: list[str] = field(default_factory=list)
    constants_passed: bool = False
    constants_reason: str = ""
    constant_lines: list[str] = field(default_factory=list)
    diagnosis: list[str] = field(default_factory=list)
    through_date: str | None = None
    gate_min_trials: float | None = None
    population_by_threshold: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""

    def summary(self) -> str:
        return "PASS" if self.passed else f"FAIL ({self.blocked_reason})"


def evaluate(wh: Warehouse, *, now: datetime | None = None) -> GateResult:
    """Run both checks in order and return the verdict."""
    from mlb_edge import integrity

    result = GateResult(evaluated_at=(now or utcnow()).isoformat())
    # Recorded even on a failing run, so a stale FAIL is distinguishable from a
    # stale PASS when someone comes back to it later.
    result.fingerprint = warehouse_fingerprint(wh)

    # --- 1. integrity --------------------------------------------------------
    checks = integrity.run_all(wh, now=now)
    errors = [
        c.line() for c in checks if not c.passed and c.severity == integrity.Severity.ERROR
    ]
    result.integrity_errors = errors
    result.integrity_passed = not errors
    if errors:
        result.blocked_reason = (
            f"{len(errors)} integrity ERROR(s); a constant fitted on a warehouse that "
            "fails its own checks is not evidence"
        )
        return result

    # --- 2. the pre-registered constant -------------------------------------
    # The qualified-hitter fit, not the shrinkage fit. The target was derived
    # from published spread among qualified hitters, so the comparison has to
    # use a comparable population -- an all-hitters fit sees more spread, which
    # pulls k down and would fail the gate for a reason that is not an error.
    latest = wh.sql(
        """
        SELECT bucket, k, through_date, min_trials
        FROM projector_constants
        WHERE player_type = ?
          AND min_trials = (
              SELECT max(min_trials) FROM projector_constants WHERE player_type = ?
          )
          AND through_date = (
              SELECT max(through_date) FROM projector_constants WHERE player_type = ?
          )
        """,
        [GATING_PLAYER_TYPE, GATING_PLAYER_TYPE, GATING_PLAYER_TYPE],
    )
    if latest.is_empty():
        result.blocked_reason = (
            f"no {GATING_PLAYER_TYPE} constants fitted yet -- run build-projections. "
            "The gate cannot pass by not having been evaluated."
        )
        return result

    fitted = {row["bucket"]: row["k"] for row in latest.iter_rows(named=True)}
    result.through_date = str(latest["through_date"][0])
    result.gate_min_trials = float(latest["min_trials"][0])

    # Every threshold, so the record carries the composition evidence rather
    # than only the verdict.
    population = wh.sql(
        """
        SELECT min_trials, k, n_players
        FROM projector_constants
        WHERE player_type = ? AND bucket = 'K' AND through_date = ?
        ORDER BY min_trials
        """,
        [GATING_PLAYER_TYPE, latest["through_date"][0]],
    )
    result.population_by_threshold = [
        {
            "min_trials": row["min_trials"],
            "k": row["k"],
            "n_players": row["n_players"],
        }
        for row in population.iter_rows(named=True)
    ]

    constant_checks = check_constants(fitted)
    result.constant_lines = [c.line() for c in constant_checks]
    passed, reason = gating_result(constant_checks)
    result.constants_passed = passed
    result.constants_reason = reason
    result.diagnosis = [
        interpret(c) for c in constant_checks if c.gating and not c.passed
    ]

    if not passed:
        result.blocked_reason = f"pre-registered constant check failed: {reason}"
        return result

    result.passed = True
    return result


def record_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / GATE_FILENAME


def write_record(result: GateResult, reports_dir: Path) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = record_path(reports_dir)
    path.write_text(json.dumps(asdict(result), indent=2, sort_keys=True), "utf-8")
    return path


def read_record(reports_dir: Path) -> dict[str, Any] | None:
    path = record_path(reports_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return None


def record_is_current(
    record: dict[str, Any] | None, wh: Warehouse
) -> tuple[bool, list[str]]:
    """Whether a stored verdict still describes the warehouse in front of us."""
    if record is None:
        return False, ["no gate record exists"]
    changes = fingerprint_diff(record.get("fingerprint"), warehouse_fingerprint(wh))
    return not changes, changes


def model_modules(package_root: Path) -> list[Path]:
    """Modules under ``model/`` that the gate governs."""
    model_dir = Path(package_root) / "model"
    if not model_dir.is_dir():
        return []
    return sorted(
        path
        for path in model_dir.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    )
