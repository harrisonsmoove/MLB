"""The gate on ``model/``.

Two conditions must hold, in order, before any simulator code exists:
integrity clean, then the pre-registered strikeout constant inside its band.

The first test here is the enforcement mechanism. The rest check that the gate
cannot be passed by accident -- by not running, by running out of order, or by
running against an empty warehouse.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest
from conftest import NOW

from mlb_edge import gate
from mlb_edge.features.preregistration import (
    EXPECTED_K_CONCENTRATION,
    RATIO_BOUNDS,
)


def test_no_model_code_without_a_passing_gate_record(request):
    """The enforcement. This is the rule, executable.

    If someone -- me, later, with momentum -- starts writing the simulator
    before the warehouse is verified and the constant checked, this fails.

    Freshness is checked too, when a warehouse is present. A verdict from
    before the warehouse was rebuilt is a statement about data that no longer
    exists. Where no warehouse is reachable (a clean clone, CI), staleness
    cannot be established and the test says so rather than pretending either
    way -- the passing-record requirement still binds.
    """
    from mlb_edge.config import load_settings
    from mlb_edge.storage.warehouse import Warehouse

    root = request.config.rootpath
    modules = gate.model_modules(root / "src" / "mlb_edge")
    if not modules:
        pytest.skip("model/ is empty; nothing to gate yet")

    record = gate.read_record(root / "reports")
    names = [m.name for m in modules]
    assert record is not None, (
        f"model/ contains {names} but reports/gate.json does not exist. "
        "Run `mlb-edge gate` against the real warehouse first."
    )
    assert record.get("passed") is True, (
        f"model/ contains {names} but the gate record says it did not pass: "
        f"{record.get('blocked_reason')}"
    )

    warehouse_path = load_settings(root).warehouse_path
    if not warehouse_path.is_file():
        pytest.skip(
            f"model/ contains {names} with a passing record, but no warehouse at "
            f"{warehouse_path} to check it against -- freshness unverified here"
        )

    warehouse = Warehouse.open(warehouse_path, read_only=True)
    try:
        current, changes = gate.record_is_current(record, warehouse)
    finally:
        warehouse.close()
    assert current, (
        f"model/ contains {names} but the gate record is stale. The warehouse "
        "has changed since it was written:\n  " + "\n  ".join(changes) +
        "\nRe-run `mlb-edge gate`."
    )


def _seed(
    warehouse,
    *,
    fitted_k: float,
    through: date = date(2025, 7, 1),
    all_hitters_k: float | None = None,
    player_type: str = "batter",
):
    """Batter constants at two thresholds, as the projector now writes them.

    ``fitted_k`` is the qualified-hitter fit -- the one the gate compares, since
    that is the population the published spread was measured on.
    """
    rows = []
    for min_trials, k_scale in ((0.0, all_hitters_k), (300.0, fitted_k)):
        for bucket, base in (("K", fitted_k), ("BB", 120.0)):
            value = k_scale if (bucket == "K" and k_scale is not None) else base
            rows.append(
                {
                    "system": "inhouse_statcast",
                    "player_type": player_type,
                    "through_date": through,
                    "bucket": bucket,
                    "min_trials": min_trials,
                    "is_primary": min_trials == 300.0,
                    "k": value,
                    "prior_mean": 0.22,
                    "saturated": False,
                    "n_players": 400 if min_trials else 900,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            )
    warehouse.load("projector_constants", pl.DataFrame(rows))


def test_gate_passes_on_a_clean_warehouse_and_a_plausible_constant(warehouse):
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION * 1.1)
    result = gate.evaluate(warehouse, now=NOW)
    assert result.integrity_passed
    assert result.constants_passed
    assert result.passed


def test_gate_blocks_on_an_implausible_constant(warehouse):
    """The synthetic-era value. Real baseball should not produce this."""
    _seed(warehouse, fitted_k=917.0)
    result = gate.evaluate(warehouse, now=NOW)
    assert result.integrity_passed
    assert not result.constants_passed
    assert not result.passed
    assert "pre-registered" in result.blocked_reason
    assert result.diagnosis, "a failure must come with a reading of what it means"


@pytest.mark.parametrize("ratio", [0.49, 2.01, 0.1, 20.0])
def test_ratios_outside_the_band_block(warehouse, ratio):
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION * ratio)
    assert not gate.evaluate(warehouse, now=NOW).passed


@pytest.mark.parametrize("ratio", [0.51, 1.0, 1.99])
def test_ratios_inside_the_band_pass(warehouse, ratio):
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION * ratio)
    assert gate.evaluate(warehouse, now=NOW).passed


def test_integrity_is_checked_first_and_short_circuits(warehouse):
    """A constant fitted on a corrupted warehouse is not evidence.

    Seeded with a constant that would otherwise pass, plus an integrity
    violation. The constant must not even be evaluated.
    """
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    warehouse.load(
        "umpire_assignments",
        pl.DataFrame(
            [
                {
                    "game_pk": 999999,  # no such game
                    "role": "Home Plate",
                    "umpire_id": 1,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.integrity_passed
    assert not result.passed
    assert not result.constants_passed
    assert result.constant_lines == [], "the constant must not be evaluated at all"


def test_gate_cannot_pass_by_never_being_evaluated(warehouse):
    """An empty constants table is a block, not a pass."""
    result = gate.evaluate(warehouse, now=NOW)
    assert result.integrity_passed
    assert not result.passed
    assert "cannot pass by not having been evaluated" in result.blocked_reason


def test_only_the_latest_snapshot_is_gated_on(warehouse):
    """An old passing fit does not license a current failing one."""
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION, through=date(2025, 6, 1))
    _seed(warehouse, fitted_k=917.0, through=date(2025, 7, 1))
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.passed
    assert result.through_date == "2025-07-01"


def test_pitcher_constants_do_not_satisfy_the_gate(warehouse):
    """The target is derived from hitter talent spread and does not transfer."""
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION, player_type="pitcher")
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.passed
    assert "no batter constants" in result.blocked_reason


def test_gate_uses_the_qualified_hitter_fit_not_the_all_hitters_one(warehouse):
    """Population must match the one the target was measured on.

    Fitting across everyone catches call-ups, widens observed spread and pulls k
    down. Gating on that against a qualified-hitter target would fail for a
    reason that is not an error.
    """
    _seed(
        warehouse,
        fitted_k=EXPECTED_K_CONCENTRATION,   # qualified fit: passes
        all_hitters_k=EXPECTED_K_CONCENTRATION * 0.2,  # all-hitters: would fail
    )
    result = gate.evaluate(warehouse, now=NOW)
    assert result.passed, "the gate must read the qualified-hitter fit"
    assert result.gate_min_trials == 300.0


def test_record_carries_the_population_evidence(warehouse):
    """The record should let you diagnose a miss, not just report one."""
    _seed(warehouse, fitted_k=917.0, all_hitters_k=300.0)
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.passed
    thresholds = {row["min_trials"] for row in result.population_by_threshold}
    assert thresholds == {0.0, 300.0}
    assert all("n_players" in row for row in result.population_by_threshold)


def test_record_round_trips(warehouse, tmp_path):
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    result = gate.evaluate(warehouse, now=NOW)
    path = gate.write_record(result, tmp_path)

    assert path.name == "gate.json"
    written = json.loads(path.read_text())
    assert written["passed"] is True
    assert gate.read_record(tmp_path)["passed"] is True


def test_missing_record_reads_as_none(tmp_path):
    assert gate.read_record(tmp_path) is None


def test_corrupt_record_reads_as_none_rather_than_raising(tmp_path):
    """A truncated record must not be mistaken for a passing one."""
    (tmp_path / "gate.json").write_text("{not json")
    assert gate.read_record(tmp_path) is None


def test_bounds_are_the_pre_registered_ones():
    """Pins the band so widening it to pass a run is a visible diff."""
    assert RATIO_BOUNDS == (0.5, 2.0)
    assert pytest.approx(55.7, abs=0.1) == EXPECTED_K_CONCENTRATION


def test_expected_k_matches_its_own_derivation():
    """The constant must equal what the documented derivation produces.

    mu = 0.22, true-talent sd = 0.055, k = mu(1-mu)/sd^2 - 1. Recomputed here so
    the number and the comment explaining it cannot drift apart.
    """
    mu, sd = 0.22, 0.055
    assert pytest.approx(mu * (1 - mu) / sd**2 - 1, abs=0.1) == EXPECTED_K_CONCENTRATION


def test_derivation_is_consistent_with_published_stabilisation():
    """An input-consistency check, and labelled as one.

    reliability(n) = n/(n+k), which is 0.5 exactly at n = k, so the
    stabilisation point IS k. (The +1 belongs to the other identity,
    sigma^2 = mu(1-mu)/(k+1), and nowhere else.) The published "~60 PA" figure
    is itself a variance decomposition -- the same relationship evaluated from a
    different published input, not a second method. Agreement means the inputs are
    mutually consistent; it is not evidence that either is right.

    Kept because inconsistency here would be informative, but it must not be
    read as corroboration. The ratio band is what does the protecting.
    """
    published_stabilisation_pa = 60.0
    assert abs(EXPECTED_K_CONCENTRATION - published_stabilisation_pa) < 10.0



# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_record_is_current_against_an_unchanged_warehouse(warehouse, tmp_path):
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    result = gate.evaluate(warehouse, now=NOW)
    gate.write_record(result, tmp_path)

    current, changes = gate.record_is_current(gate.read_record(tmp_path), warehouse)
    assert current and changes == []


def test_record_expires_when_rows_are_added(warehouse, tmp_path):
    """A verdict does not survive the data it was computed from."""
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    gate.write_record(gate.evaluate(warehouse, now=NOW), tmp_path)

    warehouse.load(
        "games",
        pl.DataFrame(
            [
                {
                    "game_pk": 776001,
                    "season": 2025,
                    "game_type": "R",
                    "game_date_local": date(2025, 4, 1),
                    "scheduled_start_ts": NOW,
                    "home_team_id": 147,
                    "away_team_id": 111,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    current, changes = gate.record_is_current(gate.read_record(tmp_path), warehouse)
    assert not current
    assert any("games" in c and "rows" in c for c in changes), changes


def test_record_expires_on_a_revision_that_adds_no_rows(warehouse, tmp_path):
    """Statcast restates values without adding rows.

    A row-count-only fingerprint would call that unchanged, and the gate would
    keep vouching for numbers that moved underneath it.
    """
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    gate.write_record(gate.evaluate(warehouse, now=NOW), tmp_path)

    warehouse.con.execute(
        "UPDATE projector_constants SET as_of_ts = ?", [NOW + timedelta(days=30)]
    )
    current, changes = gate.record_is_current(gate.read_record(tmp_path), warehouse)
    assert not current
    assert any("as_of" in c for c in changes), changes


def test_stale_record_names_what_changed(warehouse, tmp_path):
    """A staleness failure should be diagnosable, not just a refusal."""
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION)
    gate.write_record(gate.evaluate(warehouse, now=NOW), tmp_path)
    _seed(warehouse, fitted_k=EXPECTED_K_CONCENTRATION, through=date(2025, 8, 1))

    _, changes = gate.record_is_current(gate.read_record(tmp_path), warehouse)
    assert changes
    assert any("projector_constants" in c for c in changes), changes


def test_a_record_without_a_fingerprint_is_treated_as_stale(warehouse, tmp_path):
    """Records written before expiry existed must not grandfather themselves in."""
    (tmp_path / "gate.json").write_text(json.dumps({"passed": True}))
    current, changes = gate.record_is_current(gate.read_record(tmp_path), warehouse)
    assert not current
    assert "no fingerprint" in changes[0]


def test_failing_runs_also_record_a_fingerprint(warehouse, tmp_path):
    """So a stale FAIL is distinguishable from a stale PASS."""
    _seed(warehouse, fitted_k=917.0)
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.passed
    assert result.fingerprint.get("digest")
