"""Retrosheet play-string parsing.

The advancement and base-out transition matrices are estimated from these, and
the simulator uses them tens of thousands of times per game. A parser error here
would put a systematic bias into every price the system produces, so the awkward
cases get explicit tests.
"""

from __future__ import annotations

import pytest

from mlb_edge.ingest.retro_play import (
    EVENT_DOUBLE,
    EVENT_GENERIC_OUT,
    EVENT_HOME_RUN,
    EVENT_SINGLE,
    EVENT_STRIKEOUT,
    EVENT_WALK,
    HalfInningState,
    classify_basic,
    parse_advances,
    parse_play,
)


@pytest.mark.parametrize(
    "basic,expected_code",
    [
        ("S8", EVENT_SINGLE),
        ("D7", EVENT_DOUBLE),
        ("HR", EVENT_HOME_RUN),
        ("K", EVENT_STRIKEOUT),
        ("W", EVENT_WALK),
        ("63", EVENT_GENERIC_OUT),
        ("8", EVENT_GENERIC_OUT),
    ],
)
def test_classify_basic(basic, expected_code):
    assert classify_basic(basic)[0] == expected_code


def test_home_run_is_not_read_as_hit_by_pitch():
    assert classify_basic("HR")[0] == EVENT_HOME_RUN
    assert classify_basic("HP")[0] != EVENT_HOME_RUN


def test_advance_marked_out_is_an_out():
    destinations, ok = parse_advances("2XH(92)")
    assert ok and destinations == {"2": 0}


def test_advance_marked_out_with_an_error_is_safe():
    """``2XH(9E2)`` is a runner who should have been out and was not."""
    destinations, ok = parse_advances("2XH(9E2)")
    assert ok and destinations == {"2": 4}


def test_multiple_advances_split_on_semicolons():
    destinations, ok = parse_advances("3-H;2-3;1-2")
    assert ok and destinations == {"3": 4, "2": 3, "1": 2}


def test_single_with_empty_bases():
    state = HalfInningState()
    result = parse_play("S8/L", state)
    assert (result.start_base_state, result.end_base_state) == (0, 1)
    assert result.runs == 0 and result.outs == 0


def test_double_scores_the_runner_from_second():
    state = HalfInningState(runners={1: "a", 2: "b"})
    result = parse_play("D7/F.2-H;1-3", state)
    assert result.runs == 1
    assert result.end_base_state == 6, "batter on second, runner on third"
    assert result.run2_dest == 4 and result.run1_dest == 3


def test_ground_ball_double_play_records_two_outs():
    state = HalfInningState(runners={1: "a"})
    result = parse_play("64(1)3/GDP", state)
    assert result.outs == 2 and result.end_base_state == 0


def test_walk_forces_runners_along():
    state = HalfInningState(runners={1: "a", 2: "b"})
    result = parse_play("W", state)
    assert result.end_base_state == 7, "bases loaded"
    assert result.runs == 0


def test_walk_does_not_force_a_runner_on_second_alone():
    state = HalfInningState(runners={2: "b"})
    result = parse_play("W", state)
    assert result.end_base_state == 3, "runner stays on second, batter takes first"


def test_home_run_clears_the_bases_even_when_advances_are_omitted():
    state = HalfInningState(runners={1: "a", 2: "b", 3: "c"})
    result = parse_play("HR/F", state)
    assert result.runs == 4 and result.end_base_state == 0


def test_strikeout_plus_caught_stealing_is_two_outs():
    state = HalfInningState(runners={1: "a"})
    result = parse_play("K.1X2(26)", state)
    assert result.outs == 2


def test_third_out_resets_the_half_inning():
    state = HalfInningState(runners={1: "a"}, outs=2)
    parse_play("63/G", state)
    assert state.outs == 0 and state.runners == {}


def test_unparseable_event_is_flagged_not_silently_accepted():
    state = HalfInningState()
    result = parse_play("$$$", state)
    assert not result.parse_ok
