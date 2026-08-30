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
from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import utcnow

GATE_FILENAME = "gate.json"


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
    blocked_reason: str = ""

    def summary(self) -> str:
        return "PASS" if self.passed else f"FAIL ({self.blocked_reason})"


def evaluate(wh: Warehouse, *, now: datetime | None = None) -> GateResult:
    """Run both checks in order and return the verdict."""
    from mlb_edge import integrity

    result = GateResult(evaluated_at=(now or utcnow()).isoformat())

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
    latest = wh.sql(
        """
        SELECT bucket, k, through_date
        FROM projector_constants
        WHERE player_type = ?
          AND through_date = (
              SELECT max(through_date) FROM projector_constants WHERE player_type = ?
          )
        """,
        [GATING_PLAYER_TYPE, GATING_PLAYER_TYPE],
    )
    if latest.is_empty():
        result.blocked_reason = (
            f"no {GATING_PLAYER_TYPE} constants fitted yet -- run build-projections. "
            "The gate cannot pass by not having been evaluated."
        )
        return result

    fitted = {row["bucket"]: row["k"] for row in latest.iter_rows(named=True)}
    result.through_date = str(latest["through_date"][0])

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
