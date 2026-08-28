"""Resolving a book's event to an MLB ``game_pk``.

No sportsbook or prediction market publishes game_pk. They publish a commence
time and two team names, which means every market row has to be joined back to
the schedule -- and that join is the single most dangerous one in the system.

``(date, home_team, away_team)`` is not a key. It collides on every
doubleheader, and the two games of a doubleheader can have genuinely different
prices (different starters, and in a traditional twin bill a different lineup).
Silently picking the first match would attach game 2's odds to game 1's result
roughly half the time, for a couple of hundred games a season, and the resulting
CLV number would be quietly wrong in a way no aggregate would reveal.

So this resolver refuses rather than guesses. An ambiguous match returns no
game_pk and is counted. The raw payload is already cached, so improving the
matcher and re-running ``reload_from_cache`` recovers everything it declined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mlb_edge.storage.warehouse import Warehouse
from mlb_edge.timeutil import ensure_utc

# Two candidate starts closer together than this cannot be told apart by
# commence time, so a doubleheader with both games listed at the same nominal
# time is ambiguous rather than resolved.
MIN_SEPARATION = timedelta(minutes=90)


@dataclass(frozen=True)
class MatchResult:
    game_pk: int | None
    reason: str
    candidates: tuple[int, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.game_pk is not None


def normalise_team(name: str) -> str:
    """Lowercase alphanumeric form used for fuzzy-free team comparison."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class GameMatcher:
    """Resolves ``(commence_time, home, away)`` to a game_pk."""

    def __init__(self, warehouse: Warehouse, settings: Any) -> None:
        self.wh = warehouse
        aliases = (settings.section("market") or {}).get("team_aliases", {}) or {}
        self.aliases = {normalise_team(k): normalise_team(v) for k, v in aliases.items()}
        self._unresolved: list[str] = []
        self._index_cache: dict[str, int] | None = None

    @property
    def unresolved(self) -> list[str]:
        return list(self._unresolved)

    def _team_index(self) -> dict[str, int]:
        """Normalised team name/abbreviation -> team_id, across all seasons.

        Built from the warehouse rather than a literal table, so a franchise
        rename (Cleveland Indians to Guardians in 2022) resolves for the seasons
        where each name was current without anyone maintaining a list.

        Cached on the instance rather than with ``lru_cache``, which on a method
        would keep every matcher alive for the life of the process.
        """
        if self._index_cache is not None:
            return self._index_cache
        rows = self.wh.sql(
            "SELECT DISTINCT team_id, name, abbreviation FROM teams WHERE team_id IS NOT NULL"
        )
        index: dict[str, int] = {}
        for row in rows.iter_rows(named=True):
            team_id = int(row["team_id"])
            for value in (row["name"], row["abbreviation"]):
                if value:
                    index[normalise_team(value)] = team_id
                    # "New York Yankees" also arrives as "Yankees".
                    parts = str(value).split()
                    if len(parts) > 1:
                        index.setdefault(normalise_team(parts[-1]), team_id)
        self._index_cache = index
        return index

    def team_id(self, name: str) -> int | None:
        key = normalise_team(name)
        key = self.aliases.get(key, key)
        index = self._team_index()
        if key in index:
            return index[key]
        # Last resort: a unique containment match. Requiring uniqueness keeps
        # "Chicago White Sox" from matching "Chicago Cubs".
        hits = {tid for norm, tid in index.items() if key and (key in norm or norm in key)}
        return hits.pop() if len(hits) == 1 else None

    def resolve(
        self,
        *,
        commence_ts: datetime,
        home_team_text: str,
        away_team_text: str,
        tolerance: timedelta = timedelta(hours=8),
    ) -> MatchResult:
        home_id = self.team_id(home_team_text)
        away_id = self.team_id(away_team_text)
        if home_id is None or away_id is None:
            reason = f"unmapped team: home={home_team_text!r} away={away_team_text!r}"
            self._unresolved.append(reason)
            return MatchResult(None, reason)

        cutoff = ensure_utc(commence_ts)
        rows = self.wh.sql(
            """
            SELECT game_pk, scheduled_start_ts, game_number, doubleheader FROM (
                SELECT game_pk, scheduled_start_ts, game_number, doubleheader,
                       row_number() OVER (PARTITION BY game_pk ORDER BY as_of_ts DESC) rn
                FROM games
                WHERE home_team_id = ? AND away_team_id = ?
                  AND scheduled_start_ts BETWEEN ? AND ?
            ) WHERE rn = 1
            ORDER BY scheduled_start_ts
            """,
            [home_id, away_id, cutoff - tolerance, cutoff + tolerance],
        )

        if rows.is_empty():
            reason = (
                f"no scheduled game for {away_team_text} @ {home_team_text} "
                f"within {tolerance} of {cutoff.isoformat()}"
            )
            self._unresolved.append(reason)
            return MatchResult(None, reason)

        candidates = [
            (int(r["game_pk"]), ensure_utc(r["scheduled_start_ts"]))
            for r in rows.iter_rows(named=True)
        ]
        if len(candidates) == 1:
            return MatchResult(candidates[0][0], "unique")

        # Doubleheader. Rank by distance to the quoted commence time, and only
        # accept the nearest if the runner-up is far enough away to be excluded
        # on time alone.
        ranked = sorted(candidates, key=lambda c: abs(c[1] - cutoff))
        best, runner_up = ranked[0], ranked[1]
        if abs(best[1] - runner_up[1]) < MIN_SEPARATION:
            reason = (
                f"ambiguous doubleheader: {[c[0] for c in ranked]} start within "
                f"{MIN_SEPARATION} of each other; refusing to guess"
            )
            self._unresolved.append(reason)
            return MatchResult(None, reason, tuple(c[0] for c in ranked))
        return MatchResult(best[0], "doubleheader resolved by start time",
                           tuple(c[0] for c in ranked))
