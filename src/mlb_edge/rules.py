"""Playing rules that changed between seasons.

A simulator that applies today's rules to 2018 is not reproducing 2018. The
failures are quiet and they land exactly where the validation looks: the
automatic runner on second compresses the extra-innings tail, and the designated
hitter moves league run scoring by roughly a fifth of a run per game. Both would
show up as "the state machine is wrong" when the state machine is fine.

So the rules live here, keyed by season, rather than as flags that are either on
or off for all of history. Per the standing rule on silent fallbacks, a season
outside the encoded range warns rather than quietly picking a default -- an
unrecognised season is a question, not an assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Automatic runner on second to start each extra half-inning.
#: Introduced 2020, regular season only, made permanent from 2023.
RUNNER_ON_SECOND_FROM_SEASON = 2020

#: The postseason plays traditional extra innings. Encoded separately because
#: assuming the regular-season rule carries over would silently reshape every
#: postseason extra-innings distribution.
RUNNER_ON_SECOND_POSTSEASON = False

#: Designated hitter by league.
#: AL from 1973. NL had none through 2019; 2020 both leagues (shortened season);
#: 2021 the NL reverted; universal from 2022.
DH_AL_FROM_SEASON = 1973
DH_NL_SEASONS_BEFORE_UNIVERSAL = frozenset({2020})
DH_UNIVERSAL_FROM_SEASON = 2022

#: Seasons this module has been checked against. Anything outside it is a
#: question rather than a default.
ENCODED_SEASONS = range(2015, 2027)

_warned_seasons: set[int] = set()


def _warn_unencoded(season: int, what: str) -> None:
    if season in ENCODED_SEASONS or season in _warned_seasons:
        return
    _warned_seasons.add(season)
    print(
        f"[rules] WARN: season {season} is outside the encoded range "
        f"{ENCODED_SEASONS.start}-{ENCODED_SEASONS.stop - 1}; {what} is being "
        "extrapolated. Check the rule before trusting the result.",
        flush=True,
    )


def runner_on_second_applies(season: int, game_type: str = "R") -> bool:
    """Whether extra innings start with a runner on second.

    Regular season 2020 onward. The postseason does not use it, and treating it
    as universal would compress every postseason extras distribution -- a small
    number of games, but the ones with the longest tails.
    """
    _warn_unencoded(season, "the extra-innings runner rule")
    if game_type != "R":
        return RUNNER_ON_SECOND_POSTSEASON
    return season >= RUNNER_ON_SECOND_FROM_SEASON


def has_designated_hitter(season: int, league: str) -> bool:
    """Whether this league used a DH in this season.

    ``league`` is ``"AL"`` or ``"NL"``. For interleague games the rule follows
    the **home** team's league before 2022; from 2022 it is universal and the
    distinction stops mattering.
    """
    _warn_unencoded(season, "the designated hitter rule")
    normalised = (league or "").strip().upper()
    if normalised not in ("AL", "NL"):
        print(
            f"[rules] WARN: unrecognised league {league!r}; assuming no DH. "
            "Pass 'AL' or 'NL'.",
            flush=True,
        )
        return False

    if season >= DH_UNIVERSAL_FROM_SEASON:
        return True
    if normalised == "AL":
        return season >= DH_AL_FROM_SEASON
    return season in DH_NL_SEASONS_BEFORE_UNIVERSAL


def pitcher_bats(season: int, league: str) -> bool:
    """Whether a pitcher takes a turn in the batting order.

    The inverse of the DH, named separately because that is how the lineup
    builder thinks about it -- and because a pitcher's spot is worth roughly
    0.2-0.3 runs per game against a DH, which is large next to the tolerances
    the validation runs at.
    """
    return not has_designated_hitter(season, league)


@dataclass(frozen=True)
class SeasonRules:
    """Every season-varying rule the simulator needs, resolved once."""

    season: int
    game_type: str
    home_league: str

    @property
    def runner_on_second_in_extras(self) -> bool:
        return runner_on_second_applies(self.season, self.game_type)

    @property
    def designated_hitter(self) -> bool:
        return has_designated_hitter(self.season, self.home_league)

    @property
    def pitcher_in_lineup(self) -> bool:
        return not self.designated_hitter

    def describe(self) -> str:
        return (
            f"{self.season} {self.game_type} ({self.home_league} home): "
            f"DH={'yes' if self.designated_hitter else 'no'}, "
            f"extras runner={'yes' if self.runner_on_second_in_extras else 'no'}"
        )


def for_game(season: int, game_type: str, home_league: str) -> SeasonRules:
    return SeasonRules(season=season, game_type=game_type, home_league=home_league)
