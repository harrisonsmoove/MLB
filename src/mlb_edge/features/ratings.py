"""The in-house projector: true-talent PA outcome distributions.

Produces, for every hitter and pitcher, a multinomial over
``[K, BB, HBP, 1B, 2B, 3B, HR, OUT]`` -- the simulator's direct input.

The design in one paragraph: strikeouts, walks and hit-by-pitches are counted
directly, because they are discrete events the batter and pitcher control
between them. Everything else is a ball in play, and those are *not* counted
from what happened. The number of balls in play is counted, then distributed
across hit types using the league's contact-quality table applied to the
player's own exit velocities and launch angles. That substitution is the whole
point: a hitter's realised hits are mostly defence, park and luck, while his
contact quality is his own and settles several times faster.

Everything is then shrunk toward a hierarchical prior with regression constants
fit by empirical Bayes -- per bucket, because strikeout rate and batted-ball
outcomes settle at completely different speeds.

Point-in-time throughout: every snapshot reads plate appearances strictly before
its own date, and the contact table is refit on the same restricted history. A
projection dated 12 May knows nothing about 12 May.

Uncertainty survives to the output. ``n_effective`` is the Dirichlet
concentration, so a player with 40 plate appearances of history produces a
genuinely wider game distribution than one with 4000 rather than a falsely
confident point estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
import polars as pl

from mlb_edge.config import Settings
from mlb_edge.features.battedball import CONTACT_BUCKETS, BattedBallModel, fit_batted_ball_model
from mlb_edge.features.pa_outcomes import BUCKETS
from mlb_edge.features.pitcher_proxies import ProxyModel, fit_proxy_model
from mlb_edge.features.preregistration import (
    PreregistrationCheck,
    check_constants,
    gating_result,
)
from mlb_edge.features.shrinkage import (
    HierarchicalPrior,
    RegressionConstant,
    dirichlet_posterior,
    fit_regression_constant,
)
from mlb_edge.storage.warehouse import Warehouse

#: Buckets counted directly rather than derived from contact quality.
DISCRETE_BUCKETS: tuple[str, ...] = ("K", "BB", "HBP")

HANDS: tuple[str, ...] = ("L", "R")


@dataclass
class ProjectorReport:
    through_date: date
    players: int = 0
    rows: int = 0
    batted_balls: int = 0
    unmeasured_batted_balls: int = 0
    constants: dict[str, RegressionConstant] = field(default_factory=dict)
    #: Every fit, keyed by playing-time threshold. Only the primary shrinks
    #: anything; the rest exist so a missed ratio can be diagnosed as sample
    #: composition rather than mis-specification.
    constant_fits: dict[float, dict[str, RegressionConstant]] = field(default_factory=dict)
    primary_min_trials: float = 0.0
    proxy: ProxyModel | None = None

    def population(self, min_trials: float) -> int:
        """Players that informed the fit at a given threshold."""
        fit = self.constant_fits.get(min_trials, {})
        return max((c.n_players for c in fit.values()), default=0)

    def preregistration(
        self, min_trials: float | None = None
    ) -> list[PreregistrationCheck]:
        """Fitted constants against targets fixed before any data was seen.

        Defaults to the primary fit. Pass a threshold to check a diagnostic one
        -- the gate uses the qualified-hitter fit, because that is the
        population the published spread was measured on.
        """
        fit = (
            self.constants
            if min_trials is None
            else self.constant_fits.get(min_trials, self.constants)
        )
        return check_constants({name: c.k for name, c in fit.items()})

    def preregistration_passed(self, min_trials: float | None = None) -> bool:
        return gating_result(self.preregistration(min_trials))[0]

    def preregistration_lines(self, min_trials: float | None = None) -> list[str]:
        return [check.line() for check in self.preregistration(min_trials)]

    def population_lines(self) -> list[str]:
        """Ratio and hitter count at every threshold, side by side.

        If the ratio moves with the population, the miss is composition: a wider
        net catches call-ups, which widens observed spread and pulls k down.
        If it holds steady across thresholds, composition is not the story and
        the estimator itself is the suspect.
        """
        lines: list[str] = []
        for min_trials in sorted(self.constant_fits):
            checks = [c for c in self.preregistration(min_trials) if c.gating]
            if not checks:
                continue
            check = checks[0]
            marker = " *primary" if min_trials == self.primary_min_trials else ""
            lines.append(
                f"min_pa>={min_trials:<5.0f} n={self.population(min_trials):<5d} "
                f"k={check.fitted_k:>9,.0f}  ratio={check.ratio:>7.2f}{marker}"
            )
        return lines

    def summary(self) -> str:
        constants = ", ".join(
            f"{name}:k={c.k:.0f}" for name, c in sorted(self.constants.items())
        )
        return (
            f"{self.through_date}: players={self.players} rows={self.rows} "
            f"batted_balls={self.batted_balls:,} "
            f"unmeasured={self.unmeasured_batted_balls:,} [{constants}]"
            + (
                f" proxy[r2={self.proxy.r_squared:.2f} usable={self.proxy.usable}]"
                if self.proxy is not None
                else ""
            )
        )


class Projector:
    def __init__(self, settings: Settings, warehouse: Warehouse) -> None:
        self.settings = settings
        self.wh = warehouse
        self.config = settings.section("projector")

    # -- public ---------------------------------------------------------------
    def build(
        self,
        through: date,
        *,
        player_type: str = "batter",
        model: BattedBallModel | None = None,
    ) -> tuple[pl.DataFrame, ProjectorReport]:
        """Project every player as of ``through`` (exclusive)."""
        report = ProjectorReport(through_date=through)

        history = self._history(through, player_type)
        if history.is_empty():
            return pl.DataFrame(), report

        if model is None:
            model = fit_batted_ball_model(
                self.wh,
                through=through,
                pooling_k=float(self.config.get("battedball_pooling_k", 200)),
            )
        report.batted_balls = model.n_batted_balls
        report.unmeasured_batted_balls = model.n_unmeasured

        players = self._player_tallies(history, model, player_type)
        if not players:
            return pl.DataFrame(), report
        report.players = len(players)

        primary_min_trials = float(self.config.get("min_trials_for_constant_fit", 200))
        thresholds = {primary_min_trials} | {
            float(t) for t in self.config.get("diagnostic_fit_thresholds", []) or []
        }
        report.constant_fits = {
            threshold: self._fit_constants(players, threshold)
            for threshold in sorted(thresholds)
        }
        report.primary_min_trials = primary_min_trials
        constants = report.constant_fits[primary_min_trials]
        report.constants = constants
        priors = self._fit_priors(players)
        league_platoon = self._league_platoon_deltas(players)

        # Plate discipline earns a pitcher a better strikeout prior than his
        # league cell. Fit on the same restricted history as everything else.
        proxy = self._fit_proxy(players) if player_type == "pitcher" else None
        report.proxy = proxy

        rows: list[dict[str, Any]] = []
        as_of = datetime.combine(through, time.min, tzinfo=UTC)
        for tally in players.values():
            rows.extend(
                self._rows_for_player(
                    tally, constants, priors, league_platoon, through, as_of,
                    player_type, proxy,
                )
            )
        report.rows = len(rows)
        return (pl.DataFrame(rows) if rows else pl.DataFrame()), report

    def snapshot_dates(self, start: date, end: date) -> list[date]:
        cadence = int(self.config.get("snapshot_cadence_days", 7))
        dates: list[date] = []
        cursor = start
        while cursor <= end:
            dates.append(cursor)
            cursor += timedelta(days=cadence)
        return dates

    # -- history --------------------------------------------------------------
    def _history(self, through: date, player_type: str) -> pl.DataFrame:
        """Plate appearances strictly before ``through``, with recency weights.

        Intentional walks are excluded from the denominator: an intentional walk
        is a manager's judgement about the base-out state, not the hitter's
        plate discipline, and counting it as evidence of either player's skill
        would be attributing a decision to the wrong agent. The simulator
        reintroduces them from game state.
        """
        halflife = float(self.config.get("recency_halflife_days", 730))
        frame = self.wh.sql(
            """
            SELECT game_date, batter_id, pitcher_id, bat_side, pit_throws,
                   outcome, launch_speed, launch_angle,
                   pitches, swings, whiffs, called_strikes
            FROM pa_outcomes
            WHERE game_date < ? AND NOT coalesce(is_intentional_bb, FALSE)
            """,
            [through],
        )
        if frame.is_empty():
            return frame

        ages = frame.select(
            (pl.lit(through) - pl.col("game_date")).dt.total_days().alias("age")
        )["age"].to_numpy()
        weights = np.exp(-np.log(2.0) * np.maximum(ages, 0) / halflife)
        return frame.with_columns(pl.Series("weight", weights))

    def _player_tallies(
        self, history: pl.DataFrame, model: BattedBallModel, player_type: str
    ) -> dict[int, PlayerTally]:
        """Weighted per-player counts, split by opposing hand."""
        id_column = "batter_id" if player_type == "batter" else "pitcher_id"
        own_hand_column = "bat_side" if player_type == "batter" else "pit_throws"
        opp_hand_column = "pit_throws" if player_type == "batter" else "bat_side"

        tallies: dict[int, PlayerTally] = {}
        for row in history.iter_rows(named=True):
            player_id = row[id_column]
            if player_id is None:
                continue
            tally = tallies.get(player_id)
            if tally is None:
                tally = PlayerTally(player_id=player_id, hand=row[own_hand_column] or "R")
                tallies[player_id] = tally

            opposing = row[opp_hand_column]
            weight = float(row["weight"])
            outcome = row["outcome"]

            for split in ("ALL", opposing if opposing in HANDS else None):
                if split is None:
                    continue
                counts = tally.splits.setdefault(split, SplitCounts())
                counts.pa += weight
                counts.pitches += (row["pitches"] or 0) * weight
                counts.swings += (row["swings"] or 0) * weight
                counts.whiffs += (row["whiffs"] or 0) * weight
                counts.called_strikes += (row["called_strikes"] or 0) * weight
                if outcome in DISCRETE_BUCKETS:
                    counts.discrete[outcome] = counts.discrete.get(outcome, 0.0) + weight
                else:
                    counts.balls_in_play += weight
                    distribution, _ = model.predict(row["launch_speed"], row["launch_angle"])
                    for bucket in CONTACT_BUCKETS:
                        probability = distribution[bucket]
                        counts.expected_contact[bucket] = (
                            counts.expected_contact.get(bucket, 0.0) + probability * weight
                        )
                        counts.expected_contact_sumsq[bucket] = (
                            counts.expected_contact_sumsq.get(bucket, 0.0)
                            + probability * probability * weight
                        )
        return tallies

    # -- fitting --------------------------------------------------------------
    def _fit_constants(
        self, players: dict[int, PlayerTally], min_trials: float
    ) -> dict[str, RegressionConstant]:
        """One regression constant per outcome bucket, fit across all players.

        Fit on the *expected* counts rather than realised ones. Expected
        contact is less noisy, so its constants come out smaller -- which is the
        entire benefit of using contact quality, arriving here automatically
        rather than being asserted.
        """
        constants: dict[str, RegressionConstant] = {}
        for bucket in BUCKETS:
            successes: list[float] = []
            trials: list[float] = []
            noise: list[float] = []
            for tally in players.values():
                counts = tally.splits.get("ALL")
                if counts is None or counts.pa <= 0:
                    continue
                successes.append(counts.bucket_count(bucket))
                trials.append(counts.pa)
                noise.append(counts.sampling_variance(bucket))
            constants[bucket] = fit_regression_constant(
                successes,
                trials,
                name=bucket,
                min_trials=min_trials,
                noise_variance=noise,
            )
        return constants

    def _fit_priors(self, players: dict[int, PlayerTally]) -> dict[str, HierarchicalPrior]:
        """league -> handedness -> experience, per bucket."""
        pooling_k = float(self.config.get("hierarchy_pooling_k", 400))
        rookie_threshold = float(self.config.get("rookie_pa_threshold", 500))

        priors: dict[str, HierarchicalPrior] = {}
        for bucket in BUCKETS:
            rows: list[tuple[tuple[str, ...], float, float]] = []
            for tally in players.values():
                counts = tally.splits.get("ALL")
                if counts is None or counts.pa <= 0:
                    continue
                rows.append(
                    (
                        tally.prior_key(rookie_threshold),
                        counts.bucket_count(bucket),
                        counts.pa,
                    )
                )
            priors[bucket] = HierarchicalPrior(k=pooling_k).fit(rows)
        return priors

    def _fit_proxy(self, players: dict[int, PlayerTally]) -> ProxyModel:
        """Fit the plate-discipline map on the snapshot's own restricted history."""
        min_trials = float(self.config.get("min_trials_for_constant_fit", 200))
        observations: list[tuple[float, float, float, float]] = []
        for tally in players.values():
            counts = tally.splits.get("ALL")
            if counts is None or counts.pa <= 0:
                continue
            whiff, csw = counts.whiff_rate, counts.csw_rate
            if whiff is None or csw is None:
                continue
            observations.append(
                (whiff, csw, counts.discrete.get("K", 0.0) / counts.pa, counts.pa)
            )
        return fit_proxy_model(observations, min_trials=min_trials)

    def _league_platoon_deltas(
        self, players: dict[int, PlayerTally]
    ) -> dict[tuple[str, str, str], float]:
        """League-average platoon effect, keyed ``(own_hand, opposing_hand, bucket)``.

        This is what an individual platoon split regresses toward. Most hitters
        have no stable personal platoon skill at available sample sizes, so the
        league effect for their handedness pairing is very nearly the whole
        story, and treating a 90-PA personal split as signal is a classic way to
        manufacture edge that is not there.
        """
        totals: dict[tuple[str, str, str], list[float]] = {}
        overall: dict[tuple[str, str], list[float]] = {}
        for tally in players.values():
            for opposing in HANDS:
                counts = tally.splits.get(opposing)
                if counts is None or counts.pa <= 0:
                    continue
                for bucket in BUCKETS:
                    key = (tally.hand, opposing, bucket)
                    entry = totals.setdefault(key, [0.0, 0.0])
                    entry[0] += counts.bucket_count(bucket)
                    entry[1] += counts.pa
            reference = tally.splits.get("ALL")
            if reference is not None and reference.pa > 0:
                for bucket in BUCKETS:
                    key = (tally.hand, bucket)
                    entry = overall.setdefault(key, [0.0, 0.0])
                    entry[0] += reference.bucket_count(bucket)
                    entry[1] += reference.pa

        deltas: dict[tuple[str, str, str], float] = {}
        for (hand, opposing, bucket), (successes, trials) in totals.items():
            base = overall.get((hand, bucket))
            if not base or base[1] <= 0 or trials <= 0:
                continue
            deltas[(hand, opposing, bucket)] = (successes / trials) - (base[0] / base[1])
        return deltas

    # -- per player -----------------------------------------------------------
    def _rows_for_player(
        self,
        tally: PlayerTally,
        constants: dict[str, RegressionConstant],
        priors: dict[str, HierarchicalPrior],
        league_platoon: dict[tuple[str, str, str], float],
        through: date,
        as_of: datetime,
        player_type: str,
        proxy: ProxyModel | None = None,
    ) -> list[dict[str, Any]]:
        rookie_threshold = float(self.config.get("rookie_pa_threshold", 500))
        platoon_k = float(self.config.get("platoon_prior_k", 2000))
        min_pa = float(self.config.get("min_pa_for_own_row", 1.0))
        system = str(self.config.get("system_name", "inhouse_statcast"))

        overall = tally.splits.get("ALL")
        if overall is None or overall.pa < min_pa:
            return []

        prior_key = tally.prior_key(rookie_threshold)
        prior_means: dict[str, float] = {}
        prior_label = "league"
        for bucket in BUCKETS:
            value, label = priors[bucket].mean_for(prior_key)
            prior_means[bucket] = value
            prior_label = label

        # A pitcher's own whiff and called-strike rates beat his league cell as
        # a strikeout prior, and they are informative long before his strikeout
        # rate means anything. The rest of the prior is rescaled so the vector
        # still sums to one -- the strikeouts have to come from somewhere.
        if proxy is not None:
            predicted = proxy.predict(overall.whiff_rate, overall.csw_rate)
            if predicted is not None:
                remainder = 1.0 - prior_means["K"]
                if remainder > 0:
                    scale = (1.0 - predicted) / remainder
                    prior_means = {
                        b: (predicted if b == "K" else prior_means[b] * scale)
                        for b in BUCKETS
                    }
                    prior_label = f"{prior_label}+proxy"

        overall_probabilities, overall_concentration = dirichlet_posterior(
            overall.as_counts(), prior_means, constants, BUCKETS
        )

        rows = [
            self._row(
                tally, "ALL", overall_probabilities, overall_concentration, overall,
                constants, prior_label, through, as_of, system, player_type,
            )
        ]

        for opposing in HANDS:
            split = tally.splits.get(opposing)
            if split is None or split.pa <= 0:
                continue
            # The prior for "this player vs LHP" is his own overall rate moved
            # by the league platoon delta -- not the league rate vs LHP, which
            # would throw away everything already known about him.
            split_prior = {}
            for bucket in BUCKETS:
                delta = league_platoon.get((tally.hand, opposing, bucket), 0.0)
                split_prior[bucket] = max(overall_probabilities[bucket] + delta, 1e-6)
            total = sum(split_prior.values())
            split_prior = {b: v / total for b, v in split_prior.items()}

            heavy = {
                bucket: RegressionConstant(
                    name=f"{bucket}|{opposing}",
                    prior_mean=split_prior[bucket],
                    k=platoon_k,
                    n_players=constants[bucket].n_players,
                    var_observed=constants[bucket].var_observed,
                    var_binomial=constants[bucket].var_binomial,
                    var_true=constants[bucket].var_true,
                    saturated=constants[bucket].saturated,
                )
                for bucket in BUCKETS
            }
            probabilities, concentration = dirichlet_posterior(
                split.as_counts(), split_prior, heavy, BUCKETS
            )
            rows.append(
                self._row(
                    tally, opposing, probabilities, concentration, split,
                    heavy, f"{prior_label}+platoon", through, as_of, system, player_type,
                )
            )
        return rows

    def _row(
        self,
        tally: PlayerTally,
        vs_hand: str,
        probabilities: dict[str, float],
        concentration: float,
        counts: SplitCounts,
        constants: dict[str, RegressionConstant],
        prior_label: str,
        through: date,
        as_of: datetime,
        system: str,
        player_type: str,
    ) -> dict[str, Any]:
        # Share of the posterior traceable to the prior rather than to the
        # player's own record. Derived from the concentration so a saturated
        # rare bucket cannot dominate it the way an unweighted mean of the
        # constants would.
        effective_prior = max(concentration - counts.pa, 0.0)
        prior_weight = (
            effective_prior / concentration if concentration > 0 else 1.0
        )
        return {
            "system": system,
            "player_id": tally.player_id,
            "player_type": player_type,
            "vs_hand": vs_hand,
            "through_date": through,
            "p_k": probabilities["K"],
            "p_bb": probabilities["BB"],
            "p_hbp": probabilities["HBP"],
            "p_1b": probabilities["1B"],
            "p_2b": probabilities["2B"],
            "p_3b": probabilities["3B"],
            "p_hr": probabilities["HR"],
            "p_out": probabilities["OUT"],
            "n_observed": counts.pa,
            "n_effective": concentration,
            "prior_weight": prior_weight,
            "prior_cell": prior_label,
            "as_of_ts": as_of,
            "source": "projector",
            "source_partition": through.isoformat(),
            "ingested_at": as_of,
        }


def constants_frame(
    report: ProjectorReport, *, system: str, player_type: str
) -> pl.DataFrame:
    """Serialise every fitted constant, at every threshold, for storage."""
    as_of = datetime.combine(report.through_date, time.min, tzinfo=UTC)
    return pl.DataFrame(
        [
            {
                "system": system,
                "player_type": player_type,
                "through_date": report.through_date,
                "bucket": name,
                "min_trials": min_trials,
                "is_primary": min_trials == report.primary_min_trials,
                "k": constant.k,
                "prior_mean": constant.prior_mean,
                "var_observed": constant.var_observed,
                "var_binomial": constant.var_binomial,
                "var_true": constant.var_true,
                "saturated": constant.saturated,
                "n_players": constant.n_players,
                "as_of_ts": as_of,
                "source": "projector",
                "source_partition": report.through_date.isoformat(),
                "ingested_at": as_of,
            }
            for min_trials, fit in sorted(report.constant_fits.items())
            for name, constant in sorted(fit.items())
        ]
    )


@dataclass
class SplitCounts:
    """Weighted tallies for one player against one opposing hand."""

    pa: float = 0.0
    balls_in_play: float = 0.0
    discrete: dict[str, float] = field(default_factory=dict)
    expected_contact: dict[str, float] = field(default_factory=dict)
    #: Sum of squared per-ball probabilities, for the sampling variance of the
    #: expected counts. Those counts are sums of probabilities rather than of
    #: Bernoulli draws, so their noise floor is not p(1-p)/n.
    expected_contact_sumsq: dict[str, float] = field(default_factory=dict)

    # Plate-discipline counts. Every pitch contributes to these while only the
    # last pitch of a PA contributes to a strikeout, which is why they settle
    # faster and make a better prior.
    pitches: float = 0.0
    swings: float = 0.0
    whiffs: float = 0.0
    called_strikes: float = 0.0

    @property
    def whiff_rate(self) -> float | None:
        return self.whiffs / self.swings if self.swings > 0 else None

    @property
    def csw_rate(self) -> float | None:
        """Called strikes plus whiffs, over pitches."""
        return (self.called_strikes + self.whiffs) / self.pitches if self.pitches > 0 else None

    def sampling_variance(self, bucket: str) -> float:
        """Sampling variance of this player's observed rate for one bucket.

        For a discrete bucket the trials are Bernoulli, so the familiar
        p(1-p)/n applies. For a contact-derived bucket the observed value is an
        average of probabilities over the balls he happened to hit, so the
        variance is the within-player spread of those probabilities divided by
        the number of balls -- materially smaller, which is the point.
        """
        if self.pa <= 0:
            return 0.0
        if bucket in DISCRETE_BUCKETS:
            rate = self.discrete.get(bucket, 0.0) / self.pa
            return rate * (1.0 - rate) / self.pa
        if self.balls_in_play <= 0:
            return 0.0
        mean = self.expected_contact.get(bucket, 0.0) / self.balls_in_play
        mean_square = self.expected_contact_sumsq.get(bucket, 0.0) / self.balls_in_play
        within = max(mean_square - mean * mean, 0.0)
        # Scale from a per-ball rate to a per-PA rate: the expected count is
        # spread over balls in play but the denominator is plate appearances.
        return within * self.balls_in_play / (self.pa * self.pa)

    def bucket_count(self, bucket: str) -> float:
        if bucket in DISCRETE_BUCKETS:
            return self.discrete.get(bucket, 0.0)
        return self.expected_contact.get(bucket, 0.0)

    def as_counts(self) -> dict[str, float]:
        return {bucket: self.bucket_count(bucket) for bucket in BUCKETS}


@dataclass
class PlayerTally:
    player_id: int
    hand: str
    splits: dict[str, SplitCounts] = field(default_factory=dict)

    def prior_key(self, rookie_threshold: float) -> tuple[str, ...]:
        overall = self.splits.get("ALL")
        pa = overall.pa if overall else 0.0
        experience = "rookie" if pa < rookie_threshold else "established"
        return (f"hand:{self.hand}", experience)
