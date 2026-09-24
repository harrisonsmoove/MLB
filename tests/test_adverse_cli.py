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


def _odds_payload(start: datetime) -> str:
    return json.dumps([{
        "id": f"abc{start:%m%d}",
        "home_team": "Toronto Blue Jays",
        "away_team": "Baltimore Orioles",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
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


def _build(
    base: Path,
    side: str,
    yes_mid: float,
    *,
    start: datetime = FIRST_PITCH,
    ticker_date: str = "26SEP23",
    tag: str = "a",
) -> None:
    """One game, six ticks, with the ticker's YES side set to ``side``."""
    root = base / "poll"
    ticker = f"KXMLBGAME-{ticker_date}1905TORBAL-{side}"
    for index in range(6):
        stamp = start - timedelta(minutes=90 - index * 15)
        day = stamp.date().isoformat()
        _write(root, "odds", day, f"{tag}{index:02d}", [{
            "payload": _odds_payload(start), "error": None, "fetched_at": stamp,
        }])
        _write(root, "kalshi", day, f"{tag}{index:02d}", [{
            "endpoint": "orderbook", "key": ticker, "payload": _book(yes_mid),
            "error": None, "fetched_at": stamp,
        }])


def _run_probe(tmp_path: Path) -> str:
    shutil.copytree(REPO / "config", tmp_path / "config", dirs_exist_ok=True)
    (tmp_path / "config" / "local.yaml").write_text(
        f"poller:\n  archive_dir: {tmp_path / 'poll'}\n"
    )
    result = CliRunner().invoke(
        app, ["probe-inplay", "--root", str(tmp_path)]
    )
    assert result.exit_code in (0, 1), result.output
    return result.output


def _suspended_payload(start: datetime) -> str:
    """The market is still on the board, with nothing quoted on it."""
    return json.dumps([{
        "id": "susp",
        "home_team": "Toronto Blue Jays",
        "away_team": "Baltimore Orioles",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "bookmakers": [{
            "key": "pinnacle",
            "markets": [{"key": "h2h", "outcomes": []}],
        }],
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


# --- one matchup is not one game -------------------------------------------


def test_a_rematch_is_a_separate_game(archive: Path) -> None:
    """The third defect: the same two teams meet again a week later.

    Both legs were keyed on the team pair alone, with no date. Every meeting
    between two teams in the archive collapsed onto one key -- one first
    pitch, one close, one merged price series standing for three or four games
    a week. Here the 23rd agrees and the 30th is 12 points off. Merged, the
    30th's quotes all fall after the 23rd's first pitch and vanish as
    "in-play"; separated, its gaps are found.
    """
    base = archive / "rematch"
    later = FIRST_PITCH + timedelta(days=7)
    _build(base, "BAL", 1.0 - TORONTO_FAIR, tag="a")
    _build(
        base, "BAL", 1.0 - TORONTO_FAIR - 0.12,
        start=later, ticker_date="26SEP30", tag="b",
    )

    output = _run(base, dump=2, dump_min=0.05)

    assert "2 joined on both venues" in output, output
    assert "qualifying gaps above the floor: 0" not in output
    # The gaps must come from the second meeting, not the first.
    assert "KXMLBGAME-26SEP301905TORBAL-BAL" in output
    assert "KXMLBGAME-26SEP231905TORBAL-BAL" not in output


def test_both_meetings_are_counted_as_games(archive: Path) -> None:
    base = archive / "twogames"
    _build(base, "TOR", TORONTO_FAIR, tag="a")
    _build(
        base, "TOR", TORONTO_FAIR,
        start=FIRST_PITCH + timedelta(days=7), ticker_date="26SEP30", tag="b",
    )
    output = _run(base)
    assert "2 joined on both venues" in output, output
    assert "qualifying gaps above the floor: 0" in output


# --- one game, two markets -------------------------------------------------


def test_both_sides_of_one_game_are_not_two_observations(archive: Path) -> None:
    """Kalshi lists a market per side; both share an event ticker.

    Counting both doubles n with observations that are near-complements of
    each other, not independent. The gap count must match the single-market
    case, not twice it.
    """
    one = archive / "one"
    _build(one, "BAL", 1.0 - TORONTO_FAIR - 0.12, tag="a")
    single = _run(one)

    two = archive / "two"
    _build(two, "BAL", 1.0 - TORONTO_FAIR - 0.12, tag="a")
    _build(two, "TOR", TORONTO_FAIR + 0.12, tag="b")
    doubled = _run(two)

    def count(output: str) -> str:
        return output.split("qualifying gaps above the floor:")[1].split()[0]

    assert "1 joined on both venues" in doubled, doubled
    assert "game(s) had a market on both sides" in doubled
    assert count(doubled) == count(single), (
        f"both sides counted separately: {count(doubled)} vs {count(single)}"
    )


# --- the poller collects more than moneylines ------------------------------


def test_totals_markets_are_a_different_market_not_a_parse_failure(
    archive: Path,
) -> None:
    """The 297,859 "unresolvable side" drops, explained.

    The poller collects KXMLBGAME, KXMLBWINNER and KXMLBTOTAL. A totals
    ticker's suffix is its STRIKE -- ``...-11`` is eleven runs -- so reading it
    as a side yields codes 5 through 12 and no team. Reporting that as a side
    failure implies lost moneyline data; it is a different market, and tier 0
    buys no totals reference to price it against.
    """
    base = archive / "totals"
    _build(base, "BAL", 1.0 - TORONTO_FAIR - 0.12, tag="a")
    # The same game's totals ladder, which must not be read as sides.
    root = base / "poll"
    for index, strike in enumerate((8, 9, 10, 11, 12)):
        stamp = FIRST_PITCH - timedelta(minutes=90 - index * 15)
        _write(root, "kalshi", stamp.date().isoformat(), f"t{index:02d}", [{
            "endpoint": "orderbook",
            "key": f"KXMLBTOTAL-26SEP231905TORBAL-{strike}",
            "payload": _book(0.45), "error": None, "fetched_at": stamp,
        }])

    output = _run(base)

    assert "not the game-winner market" in output
    assert "KXMLBTOTAL" in output
    assert "strike" in output  # the table wraps the full phrase
    # And the moneyline result is untouched by their presence.
    assert "1 joined on both venues" in output
    assert "qualifying gaps above the floor: 0" not in output


def test_a_totals_ladder_is_not_reported_as_unresolvable_sides(
    archive: Path,
) -> None:
    base = archive / "nosides"
    _build(base, "BAL", 1.0 - TORONTO_FAIR, tag="a")
    root = base / "poll"
    for index, strike in enumerate((8, 9, 10)):
        stamp = FIRST_PITCH - timedelta(minutes=90 - index * 15)
        _write(root, "kalshi", stamp.date().isoformat(), f"t{index:02d}", [{
            "endpoint": "orderbook",
            "key": f"KXMLBTOTAL-26SEP231905TORBAL-{strike}",
            "payload": _book(0.45), "error": None, "fetched_at": stamp,
        }])

    output = _run(base)

    assert "could not be resolved" not in output, (
        "totals strikes were reported as failed side resolution"
    )


# --- probe-inplay: listed is not priced ------------------------------------


def test_a_listed_but_suspended_market_is_not_an_in_play_quote(
    archive: Path,
) -> None:
    """Why the two outputs disagreed.

    probe-inplay counted a book whenever an h2h market was LISTED after first
    pitch, and reported in-play coverage across nearly every book up to 319
    minutes past commence. adverse-selection counts only markets with two
    priced outcomes, and saw 2,675. Both were measuring, but not the same
    thing, and the looser one was mine.
    """
    base = archive / "suspended"
    _build(base, "BAL", 1.0 - TORONTO_FAIR, tag="a")
    root = base / "poll"
    for index in range(3):
        stamp = FIRST_PITCH + timedelta(minutes=20 * (index + 1))
        _write(root, "odds", stamp.date().isoformat(), f"z{index}", [{
            "payload": _suspended_payload(FIRST_PITCH),
            "error": None, "fetched_at": stamp,
        }])

    output = _run_probe(base)

    # Assert on the verdict lines, not the table: rich wraps narrow cells.
    assert "none carry prices" in output
    assert "(0 across all books; 0 for pinnacle)" in output
    assert "In-play quotes exist" not in output


def test_a_genuinely_priced_in_play_market_reads_as_such(archive: Path) -> None:
    base = archive / "livequotes"
    _build(base, "BAL", 1.0 - TORONTO_FAIR, tag="a")
    root = base / "poll"
    for index in range(3):
        stamp = FIRST_PITCH + timedelta(minutes=20 * (index + 1))
        _write(root, "odds", stamp.date().isoformat(), f"z{index}", [{
            "payload": _odds_payload(FIRST_PITCH),
            "error": None, "fetched_at": stamp,
        }])

    output = _run_probe(base)

    assert "In-play quotes exist" in output
    assert "(3 across all books; 3 for pinnacle)" in output
