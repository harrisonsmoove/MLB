"""The schema registry's promises, enforced.

These are the tests that make "point-in-time integrity verified by an automated
test suite" mean something. They are written against the registry rather than a
hand-listed set of tables, so a new table cannot be added without declaring how
it behaves under a point-in-time read.
"""

from __future__ import annotations

import pytest

from mlb_edge.storage import schema
from mlb_edge.storage.schema import TableKind


def test_every_table_has_as_of_or_a_written_exemption():
    for spec in schema.TABLES:
        if spec.as_of_column is None:
            assert spec.kind == TableKind.META, (
                f"{spec.name} has no as_of column but is not META. Every table that "
                "can inform a feature needs an as-of axis."
            )
            assert spec.exempt_reason, f"{spec.name} is exempt without a written reason"
        else:
            assert spec.as_of_column == "as_of_ts", (
                f"{spec.name} names its as-of column {spec.as_of_column!r}. The name is "
                "uniform on purpose: one column name means one guard covers every table."
            )


def test_ddl_creates_and_contains_declared_columns(warehouse):
    for spec in schema.TABLES:
        columns = set(warehouse.columns(spec.name))
        assert columns, f"{spec.name} was not created"
        if spec.as_of_column:
            assert spec.as_of_column in columns
        for key_column in spec.key:
            assert key_column in columns, f"{spec.name} key column {key_column} missing from DDL"


def test_required_columns_are_a_subset_of_the_ddl(warehouse):
    for spec in schema.TABLES:
        if not spec.required_columns:
            continue
        columns = set(warehouse.columns(spec.name))
        missing = set(spec.required_columns) - columns
        assert not missing, f"{spec.name} declares required columns not in its DDL: {missing}"


def test_no_table_is_keyed_on_date_and_team_names():
    """Ground rule 4, enforced structurally rather than by memory."""
    forbidden = {"home_team", "away_team", "home_team_name", "away_team_name"}
    for spec in schema.TABLES:
        overlap = forbidden & set(spec.key)
        assert not overlap, (
            f"{spec.name} is keyed on {overlap}. Team names plus a date collide on "
            "doubleheaders; key on game_pk."
        )


def test_game_keyed_tables_use_game_pk(warehouse):
    """A table with a game_pk column must include it in its key."""
    for spec in schema.TABLES:
        if spec.kind == TableKind.META:
            continue
        if "game_pk" in warehouse.columns(spec.name) and spec.name not in {
            "retrosheet_events",  # keyed on retro_game_id; game_pk is a nullable link
            "pitcher_appearances",
        }:
            assert "game_pk" in spec.key, f"{spec.name} has game_pk but does not key on it"


def test_kind_partition_is_exhaustive():
    readable = {s.name for s in schema.feature_readable_tables()}
    quarantined = {s.name for s in schema.quarantined_tables()}
    meta = {s.name for s in schema.TABLES if s.kind == TableKind.META}
    assert readable & quarantined == set()
    assert readable | quarantined | meta == {s.name for s in schema.TABLES}


def test_closing_lines_are_quarantined():
    assert schema.get("closing_lines").kind == TableKind.CLOSING
    assert not schema.get("closing_lines").feature_readable


def test_outcomes_are_quarantined():
    for name in ("game_results", "pitcher_game_stats", "batter_game_stats"):
        assert schema.get(name).kind == TableKind.OUTCOME
        assert not schema.get(name).feature_readable


def test_unknown_table_raises():
    with pytest.raises(KeyError):
        schema.get("not_a_table")
