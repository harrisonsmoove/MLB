"""The study end to end, on an archive built with a known answer.

The side inversion did not live in the arithmetic -- ``find_gaps`` was always
correct on quotes that carried their team. It lived in the wiring between the
archive and the arithmetic, where the Kalshi mid was stored under an unordered
matchup key that deliberately discarded which team it was the probability of.
Unit tests on the module could not see that. These build a two-game archive
where one ticker's YES side is the home team and the other's is the away team,
and assert the study reports no gap on either.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from mlb_edge.cli import app

REPO = Path(__file__).resolve().parent.parent
FIRST_PITCH = datetime(2026, 9, 23, 23, 5, tzinfo=UTC)

# Sharp: Toronto -164 / Baltimore +140. Devigged, Toronto is about 0.605.
TORONTO_HOME = -164.0
BALTIMORE_AWAY = 140.0
#: The devigged Toronto probability these prices imply, near enough for a
#: floor comparison. Checked against devig_all in the assertion below.
TORONTO_FAIR = 0.605


def _write(root: Path, venue: str, day: str, name: str, rows: list[dict]) -> None:
    folder = root / f"venue={venue}" / f"dt={day}"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(folder / f"{venue}-{name}.parquet")


def _odds_payload(stamp: datetime) -> str:
    return json.dumps([{
        "id": "abc123",
        "home_team": "Toronto Blue Jays",
        "away_team": "Baltimore Orioles",
        "commence_time": FIRST_PITCH.isoformat().replace("+00:00", "Z"),
        "bookmakers": [{
            "key": "pinnacle",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Toronto Blue Jays", "price": TORONTO_HOME},
                {"name": "Baltimore Orioles", "price": BALTIMORE_AWAY},
            ]}],
        }],
    }])


def _book(yes: float) -> str:
    """A two-sided book whose mid is ``yes``, with a 2c spread."""
    return json.dumps({"orderbook_fp": {
        "yes_dollars": [[f"{yes - 0.01:.4f}", "1000.00"]],
        "no_dollars": [[f"{1.0 - yes - 0.01:.4f}", "1000.00"]],
    }})


def _build(base: Path, side: str, yes_mid: float) -> None:
    """One matchup, six ticks, with the ticker's YES side set to ``side``."""
    root = base / "poll"
    ticker = f"KXMLBGAME-26SEP231905TORBAL-{side}"
    for index in range(6):
        stamp = FIRST_PITCH - timedelta(minutes=90 - index * 15)
        day = stamp.date().isoformat()
        _write(root, "odds", day, f"{index:02d}", [{
            "payload": _odds_payload(stamp), "error": None, "fetched_at": stamp,
        }])
        _write(root, "kalshi", day, f"{index:02d}", [{
            "endpoint": "orderbook", "key": ticker, "payload": _book(yes_mid),
            "error": None, "fetched_at": stamp,
        }])


def _run(tmp_path: Path, **options: object) -> str:
    # The real config tree, with only the archive location overridden -- so
    # this exercises the same settings the box runs with.
    shutil.copytree(REPO / "config", tmp_path / "config", dirs_exist_ok=True)
    (tmp_path / "config" / "local.yaml").write_text(
        f"poller:\n  archive_dir: {tmp_path / 'poll'}\n"
    )
    flags = ["adverse-selection", "--root", str(tmp_path), "--min-gaps", "1"]
    for key, value in options.items():
        flag = "--" + key.replace("_", "-")
        flags += [flag] if value is True else [flag, str(value)]
    result = CliRunner().invoke(app, flags)
    assert result.exit_code in (0, 1), result.output
    return result.output


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return tmp_path


def test_the_devigged_price_is_where_this_test_thinks_it_is() -> None:
    """Pin the fixture's own arithmetic, so a failure below is the wiring."""
    from mlb_edge.market.devig import devig_all
    from mlb_edge.market.prices import american_to_prob

    fair = devig_all([
        american_to_prob(TORONTO_HOME), american_to_prob(BALTIMORE_AWAY)
    ])
    assert fair["shin"][0] == pytest.approx(TORONTO_FAIR, abs=0.01)


def test_a_home_side_ticker_in_agreement_reports_no_gap(archive: Path) -> None:
    _build(archive / "a", "TOR", TORONTO_FAIR)
    output = _run(archive / "a")
    assert "qualifying gaps above the floor: 0" in output


def test_an_away_side_ticker_in_agreement_reports_no_gap(archive: Path) -> None:
    """The regression, end to end.

    Baltimore's YES side at 0.395 is the same market view as Toronto at 0.605.
    Before the fix this was compared against 0.605 directly and produced a
    21-point gap -- which is the shape of the 303,372 gaps averaging 20pp.
    """
    _build(archive / "b", "BAL", 1.0 - TORONTO_FAIR)
    output = _run(archive / "b")
    assert "qualifying gaps above the floor: 0" in output, (
        "an away-side ticker was compared against the home probability"
    )


def test_a_real_disagreement_survives_on_an_away_side_ticker(archive: Path) -> None:
    """Orientation must not suppress genuine gaps."""
    _build(archive / "c", "BAL", 1.0 - TORONTO_FAIR - 0.06)
    output = _run(archive / "c")
    assert "qualifying gaps above the floor: 0" not in output


def test_an_implausible_gap_is_reported_as_excluded(archive: Path) -> None:
    _build(archive / "d", "BAL", 1.0 - TORONTO_FAIR - 0.30)
    output = _run(archive / "d")
    assert "plausibility bound" in output
    assert "EXCLUDED" in output


def test_the_dump_prints_one_record_end_to_end(archive: Path) -> None:
    _build(archive / "e", "BAL", 1.0 - TORONTO_FAIR - 0.12)
    output = _run(archive / "e", dump=3, dump_min=0.05)
    for field in (
        "ticker", "ticker YES side", "best yes bid", "best no bid",
        "kalshi mid", "sharp raw", "devigged", "compared as", "gap",
    ):
        assert field in output, f"{field!r} missing from the dump"
    assert "KXMLBGAME-26SEP231905TORBAL-BAL" in output
    assert "Baltimore Orioles" in output
