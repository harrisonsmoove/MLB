"""The gate on ``model/``.

Two conditions must hold, in order, before any simulator code exists:
integrity clean, then the pre-registered strikeout constant inside its band.

The first test here is the enforcement mechanism. The rest check that the gate
cannot be passed by accident -- by not running, by running out of order, or by
running against an empty warehouse.
"""

from __future__ import annotations

import json
from datetime import date

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
    """
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


def _seed(warehouse, *, fitted_k: float, through: date = date(2025, 7, 1)):
    """A warehouse carrying one batter constant and nothing that breaks integrity."""
    warehouse.load(
        "projector_constants",
        pl.DataFrame(
            [
                {
                    "system": "inhouse_statcast",
                    "player_type": "batter",
                    "through_date": through,
                    "bucket": bucket,
                    "k": k,
                    "prior_mean": 0.22,
                    "saturated": False,
                    "n_players": 400,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
                for bucket, k in (("K", fitted_k), ("BB", 120.0))
            ]
        ),
    )


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
    warehouse.load(
        "projector_constants",
        pl.DataFrame(
            [
                {
                    "system": "inhouse_statcast",
                    "player_type": "pitcher",
                    "through_date": date(2025, 7, 1),
                    "bucket": "K",
                    "k": EXPECTED_K_CONCENTRATION,
                    "saturated": False,
                    "as_of_ts": NOW,
                    "source": "test",
                    "ingested_at": NOW,
                }
            ]
        ),
    )
    result = gate.evaluate(warehouse, now=NOW)
    assert not result.passed
    assert "no batter constants" in result.blocked_reason


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


def test_derivation_agrees_with_published_stabilisation():
    """Independent corroboration, kept as a test.

    For a beta-binomial, reliability is n/(n+k), so k is the stabilisation
    point. Published work puts strikeout rate at roughly 60 PA. The derivation
    starts from talent spread instead and lands at 56. Two unrelated routes
    agreeing is the main reason to trust the target.
    """
    published_stabilisation_pa = 60.0
    assert abs(EXPECTED_K_CONCENTRATION - published_stabilisation_pa) < 10.0
