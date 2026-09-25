"""Did the gap resolve toward the sharp price, or through it?

The question stage one cannot answer. A gap distribution looks the same whether
Kalshi is stale (our edge) or informed (theirs), and on a thin board the second
is common. See reports/adverse-selection.md for the pre-registration; this is
the arithmetic.

The output is **realised convergence in probability points**, not a win rate.
The archive holds how far each gap moved, so there is no need to pick between
the two loss models that bracket a win-rate threshold and disagree by up to 35
points. Measure the thing they were approximating.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mlb_edge.market.devig import DevigError, devig_all


#: Kalshi's fee, which is part of the floor a gap must clear.
def fee(price: float) -> float:
    return 0.07 * price * (1.0 - price)


def floor_for(price: float, *, half_spread: float = 0.01, alignment: float = 0.0) -> float:
    """The gap size below which there is nothing to take. See the gate doc."""
    return fee(price) + half_spread + alignment


#: No honest pre-game gap between two venues pricing the same baseball game is
#: this large. MLB moneylines live between about 0.25 and 0.80; two venues
#: disagreeing by more than this are not disagreeing, they are being compared
#: wrongly -- most often by orientation, where the error is exactly ``1 - 2p``
#: and so looks like a huge, plausible, profitable edge. Gaps above the bound
#: are excluded and counted, never silently included.
MAX_PLAUSIBLE_GAP = 0.25


@dataclass(frozen=True)
class Quote:
    """One side's price at one moment.

    ``team`` is the canonical name of the team this price is the probability
    **of**. It is not decoration: a Kalshi YES price and a devigged sharp price
    are only comparable once both are known to refer to the same team, and
    ``orient`` is the only sanctioned way to line them up.
    """

    at: datetime
    price: float
    team: str | None = None
    #: Free-form provenance for the record dump: ticker, raw book, raw prices.
    meta: dict = field(default_factory=dict, compare=False)


@dataclass
class Gap:
    """A moment where the sharp price and Kalshi disagreed by more than the floor."""

    game_pk: int
    at: datetime
    kalshi: float
    sharp: float
    floor: float
    minutes_to_first_pitch: float
    #: Kalshi's price later, keyed by horizon label.
    later: dict[str, float] = field(default_factory=dict)
    #: Provenance carried from the two quotes, for ``--dump``.
    meta: dict = field(default_factory=dict)

    @property
    def in_play(self) -> bool:
        """The game had already started when this gap was observed.

        In-play Kalshi prices against a pre-match sharp quote are not a gap in
        any tradeable sense -- the sharp quote stopped updating at first pitch,
        so the "disagreement" is just the game happening.
        """
        return self.minutes_to_first_pitch <= 0.0

    @property
    def size(self) -> float:
        return abs(self.sharp - self.kalshi)

    @property
    def direction(self) -> int:
        """+1 when the sharp price is above Kalshi, -1 below."""
        return 1 if self.sharp > self.kalshi else -1

    def convergence(self, horizon: str) -> float | None:
        """Signed movement toward the sharp price, in probability points.

        Positive means Kalshi moved our way. Negative means it moved away, and
        we were the stale side. Capped at nothing: a move *through* the sharp
        price counts its full distance, because overshoot is information about
        who was right, not noise to be clipped.
        """
        after = self.later.get(horizon)
        if after is None:
            return None
        return (after - self.kalshi) * self.direction


@dataclass
class ConvergenceResult:
    horizon: str
    observations: int
    mean: float
    median: float
    floor: float
    toward: int

    @property
    def clears_floor(self) -> bool:
        return self.observations > 0 and self.mean > self.floor

    @property
    def negative(self) -> bool:
        """The hard stop: the archive says we are the slow side."""
        return self.observations > 0 and self.mean < 0.0

    @property
    def toward_rate(self) -> float:
        return self.toward / self.observations if self.observations else 0.0

    def line(self) -> str:
        return (
            f"{self.horizon:<26s} n={self.observations:<5d} "
            f"mean {self.mean * 100:+6.2f}pp  median {self.median * 100:+6.2f}pp  "
            f"toward {self.toward_rate * 100:5.1f}%  floor {self.floor * 100:5.2f}pp"
        )


def episodes(gaps: list[Gap], quiet: timedelta = timedelta(minutes=45)) -> int:
    """How many distinct gap EVENTS these observations represent.

    A gap that persists across snapshots is one opportunity observed several
    times, not several opportunities. At a 15-minute cadence a 90-minute
    dislocation is six rows; at two minutes it is forty-five. The row count
    therefore scales with polling frequency while the information does not,
    and a count threshold like ``--min-gaps`` can be satisfied purely by
    polling faster. This collapses runs within one game separated by less than
    ``quiet`` into a single episode.
    """
    if not gaps:
        return 0
    by_game: dict[int, list[datetime]] = {}
    for gap in gaps:
        by_game.setdefault(gap.game_pk, []).append(gap.at)
    total = 0
    for stamps in by_game.values():
        stamps.sort()
        total += 1
        for earlier, later in zip(stamps, stamps[1:], strict=False):
            if later - earlier > quiet:
                total += 1
    return total


def breakeven_toward_rate(gap: float, friction: float, *, adverse: bool = True) -> float:
    """The resolve-toward rate a gap of this size needs to break even.

    ``adverse`` chooses which loss model: a gap that resolves through costs the
    full gap (True) or nothing (False). They bracket reality and disagree by up
    to 35 points, which is why the gate does not use this -- it is reported so
    a win rate is never read without the threshold it has to clear.
    """
    if gap <= 0:
        return 1.0
    rate = 0.5 + friction / (2.0 * gap) if adverse else friction / gap
    return min(rate, 1.0)


def orient(price: float, quote_team: str | None, reference_team: str | None) -> float | None:
    """Restate ``price`` as the probability of ``reference_team`` winning.

    A two-outcome market has two ways to name the same number, and they differ
    by ``1 - 2p``. Returns ``None`` when either side is unknown: an unresolvable
    orientation is a refusal, not a coin flip. Every comparison in this module
    goes through here.
    """
    if quote_team is None or reference_team is None:
        return None
    if quote_team == reference_team:
        return price
    return 1.0 - price


@dataclass
class ScanCounts:
    """Why quotes were dropped, so a shrinking n is always explainable."""

    in_play: int = 0
    unoriented: int = 0
    stale: int = 0
    implausible: int = 0
    under_floor: int = 0
    #: ``(difference, price)`` for every gap that fell under the floor, so the
    #: near-misses can be reported by band and by price. Bounded by the
    #: under-floor count, which is tens of thousands of float pairs.
    near_misses: list[tuple[float, float]] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"in-play {self.in_play:,}   unoriented {self.unoriented:,}   "
            f"stale {self.stale:,}   under floor {self.under_floor:,}   "
            f"implausible {self.implausible:,}"
        )


def kalshi_mid(yes_levels: list[tuple[float, float]], no_levels: list[tuple[float, float]]) -> float | None:
    """Mid price implied by the two sides of a Kalshi book.

    Kalshi quotes both sides as bids. The best yes bid and the best no bid are
    two views of the same probability: a no bid at ``n`` is a yes offer at
    ``1 - n``. The mid sits between them, and a book missing either side has no
    mid rather than a one-sided guess.
    """
    if not yes_levels or not no_levels:
        return None
    best_yes = max(price for price, _ in yes_levels)
    best_no = max(price for price, _ in no_levels)
    yes_ask = 1.0 - best_no
    if not (0.0 < best_yes < 1.0) or not (0.0 < yes_ask < 1.0):
        return None
    if yes_ask < best_yes:
        # Crossed book: stale or mid-update. Refuse rather than invent a mid.
        return None
    return (best_yes + yes_ask) / 2.0


def sharp_fair(home_prob: float, away_prob: float) -> dict[str, float]:
    """Devigged home probability under every distinct method.

    On a two-outcome market additive and Shin coincide exactly, so this is
    three numbers under four names. Returned keyed by method anyway, because
    the caller reports the spread and collapsing them here would hide it.
    """
    try:
        return {name: probs[0] for name, probs in devig_all([home_prob, away_prob]).items()}
    except DevigError:
        return {}


def find_gaps(
    kalshi: list[Quote],
    sharp: list[Quote],
    *,
    game_pk: int,
    first_pitch: datetime,
    half_spread: float = 0.01,
    alignment: float = 0.0,
    max_staleness: timedelta = timedelta(minutes=30),
    max_gap: float | None = MAX_PLAUSIBLE_GAP,
    pregame_only: bool = True,
    counts: ScanCounts | None = None,
) -> list[Gap]:
    """Every moment where the two disagreed by more than the floor.

    Each Kalshi quote is paired with the most recent sharp quote at or before
    it -- never a later one, which would be looking into the future -- and
    dropped if that quote is older than ``max_staleness``.

    Both prices are restated as the probability of the **sharp quote's** team
    before they are subtracted. A Kalshi quote whose team cannot be resolved is
    dropped and counted, because comparing it anyway would be wrong by
    ``1 - 2p`` in whichever direction flatters the result.

    ``pregame_only`` drops quotes at or after first pitch: the sharp side stops
    updating there, so anything later measures the game, not a gap. ``max_gap``
    excludes and counts disagreements too large to be real.
    """
    if not kalshi or not sharp:
        return []
    tally = counts if counts is not None else ScanCounts()
    ordered = sorted(sharp, key=lambda q: q.at)
    gaps: list[Gap] = []

    index = 0
    for quote in sorted(kalshi, key=lambda q: q.at):
        if pregame_only and quote.at >= first_pitch:
            tally.in_play += 1
            continue
        while index + 1 < len(ordered) and ordered[index + 1].at <= quote.at:
            index += 1
        reference = ordered[index]
        if reference.at > quote.at or quote.at - reference.at > max_staleness:
            tally.stale += 1
            continue
        price = orient(quote.price, quote.team, reference.team)
        if price is None:
            tally.unoriented += 1
            continue
        bar = floor_for(price, half_spread=half_spread, alignment=alignment)
        difference = abs(reference.price - price)
        if difference <= bar:
            tally.under_floor += 1
            tally.near_misses.append((difference, price))
            continue
        if max_gap is not None and difference > max_gap:
            tally.implausible += 1
            continue
        gaps.append(
            Gap(
                game_pk=game_pk,
                at=quote.at,
                kalshi=price,
                sharp=reference.price,
                floor=bar,
                minutes_to_first_pitch=(first_pitch - quote.at).total_seconds() / 60.0,
                meta={
                    "team": reference.team,
                    "kalshi_as_quoted": quote.price,
                    "kalshi_team": quote.team,
                    **quote.meta,
                    **reference.meta,
                },
            )
        )
    return gaps


def attach_outcomes(
    gaps: list[Gap],
    kalshi: list[Quote],
    *,
    first_pitch: datetime,
    snapshot: timedelta = timedelta(minutes=15),
) -> None:
    """Fill in where Kalshi's price went after each gap.

    Binary search over one sorted timestamp list. The first version ran a list
    comprehension per gap per horizon, allocating a fresh list of up to every
    quote in the matchup each time -- fine on a synthetic series, and a large
    part of what made this unrunnable on 465,176 real rows.
    """
    import bisect

    ordered = sorted(kalshi, key=lambda q: q.at)
    if not ordered:
        return
    stamps = [q.at for q in ordered]

    def priced(quote: Quote, gap: Gap) -> float | None:
        """The later price, stated as the probability of the gap's team.

        The gap's own price was oriented on the way in; a later quote from the
        same matchup can name the other side, so it is oriented too. Without
        this, convergence would be measured against a number that flips sign.
        """
        return orient(quote.price, quote.team, gap.meta.get("team"))

    closing: Quote | None = None
    closing_at: datetime | None = None
    cut = bisect.bisect_right(stamps, first_pitch)
    if cut:
        closing, closing_at = ordered[cut - 1], stamps[cut - 1]

    for gap in gaps:
        for label, delay in (("+1 snapshot", snapshot), ("+1 hour", snapshot * 4)):
            index = bisect.bisect_left(stamps, gap.at + delay)
            if index < len(ordered):
                later = priced(ordered[index], gap)
                if later is not None:
                    gap.later[label] = later
        if closing is not None and closing_at is not None and closing_at > gap.at:
            settled = priced(closing, gap)
            if settled is not None:
                gap.later["close"] = settled
                # The closing book's own spread. A book that is wide when the
                # market opens and tight at the close produces a toward-rate
                # near 100% with no edge present at all, because the early
                # "gap" was mostly noise in a mid nobody could trade on. The
                # two spreads side by side are what tells that apart from a
                # real one.
                gap.meta["close_best_yes"] = closing.meta.get("best_yes")
                gap.meta["close_best_no"] = closing.meta.get("best_no")


def summarise(gaps: list[Gap], horizon: str) -> ConvergenceResult:
    moves = [(g, g.convergence(horizon)) for g in gaps]
    usable = [(g, m) for g, m in moves if m is not None]
    if not usable:
        return ConvergenceResult(horizon, 0, 0.0, 0.0, 0.0, 0)
    values = [m for _, m in usable]
    return ConvergenceResult(
        horizon=horizon,
        observations=len(values),
        mean=statistics.fmean(values),
        median=statistics.median(values),
        floor=statistics.fmean([g.floor for g, _ in usable]),
        toward=sum(1 for v in values if v > 0),
    )
