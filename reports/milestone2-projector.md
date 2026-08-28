# Milestone 2a — The in-house projector

**Date:** 2026-08-28
**Status:** Built and validated against synthetic ground truth. **Never run on real baseball data.**

---

## 1. What it does

For every hitter and pitcher, produce a multinomial over
`[K, BB, HBP, 1B, 2B, 3B, HR, OUT]` — the simulator's direct input — from
Statcast, point-in-time, with expanding windows.

The central design choice, in one sentence: **strikeouts, walks and
hit-by-pitches are counted; balls in play are not.** The *number* of balls in
play is counted, then distributed across hit types using a league-wide
contact-quality table applied to the player's own exit velocities and launch
angles.

That substitution is the whole point. A .380 batting average on balls in play
over 200 batted balls is mostly the defence he faced, the parks he played in,
and luck. His exit velocity is his own, and it settles several times faster.
Two hitters with identical contact get identical expected outcomes regardless of
who was playing left field.

### Pipeline

```
statcast_pitches  ──▶  pa_outcomes  ──▶  battedball_lookup  ──▶  pa_rates
   (pitch level)       (one row/PA)      (EV/LA → outcome)      (the simulator's input)
```

```bash
mlb-edge build-pa-outcomes
mlb-edge build-projections --start 2021-04-01 --end 2026-09-28
```

### Decisions taken from your answers

- **Statcast 2015–2026.** Config now separates data coverage (2015+) from the
  walk-forward evaluation window (2021+). Earlier seasons exist to populate
  priors, not to be scored on. A regime break is documented: pitch tracking
  moved Trackman → Hawk-Eye for 2020, which matters most for release point and
  spin. Exit velocity and launch angle are the most stable measurements across
  that change, which is part of why the projector leans on them.
- **Hierarchical prior**, `league → handedness → experience`, with partial
  pooling. Thin cells fall back toward their parent rather than being estimated
  off a handful of players.
- **Observable proxies for pitchers.** Whiff rate and called-strike-plus-whiff
  rate enter as the *prior* for strikeout rate rather than as a post-hoc blend —
  one coherent posterior instead of two estimates glued together. Every pitch
  informs a whiff rate while only the last pitch of a PA informs a strikeout, so
  the proxy is meaningful months earlier.
- **Preseason files**: waiting on your sample. The slot is clean.

### Regression constants are fit, not looked up

The "stabilisation points" that circulate (K% at 60 PA, BABIP at 800+) are
era-specific and definition-specific. Every one here is estimated by
empirical-Bayes method of moments on the beta-binomial, per bucket, at every
snapshot.

**Stated bias:** restricting the fit to players above a playing-time threshold
selects on skill, compressing apparent talent spread and pushing k upward —
toward over-regression. Letting 5-PA call-ups set the variance is worse. The
threshold is configurable and the direction is documented rather than hidden.

---

## 2. Two bugs worth describing

Both would have failed silently — producing plausible output while the
projector did nothing useful. Both are now pinned by regression tests.

### 2.1 A saturated bucket swamping the multinomial

The textbook Dirichlet update forms pseudo-counts `α_b = x_b + k_b·prior_b` and
normalises. That is only valid when the constants are equal. When a bucket
*saturates* — its observed spread never exceeded binomial noise, so k is
enormous — it contributes thousands of pseudo-counts. A hit-by-pitch rate of
1% was taking a double-digit share of hitters' entire outcome distributions.

Fix: shrink each **rate** to a proper value in [0,1] first, renormalise
afterwards. Concentration is now the probability-weighted harmonic mean of
`n + k_b`, an inverse-variance combination, so a saturated rare bucket
contributes in proportion to its tiny share rather than its huge constant.

### 2.2 The wrong noise floor on expected counts — the more dangerous one

`var_binomial = p(1-p)·E[1/n]` assumes each trial is a Bernoulli draw. A
player's expected home-run count is a sum of **probabilities** over his batted
balls, not a sum of zeros and ones, and its sampling variance is far below the
binomial floor.

Using the binomial floor over-subtracted, drove `var_true` negative, and
saturated **every contact-derived bucket**. The projector would have regressed
every hitter's contact profile to league average — its entire premise inert —
while still emitting well-formed, plausible-looking distributions.

Fix: a per-player sampling variance computed from the within-player spread of
the per-ball probabilities, passed explicitly. Before: 1B, 2B, 3B, HR and OUT
all saturated at k=100,000. After: k = 3,254 / 9,177 / 100,000 / 11,641 / 705.

This is the failure mode I would watch for elsewhere in the system: an
estimator that is wrong in a way that produces *more* confident-looking output,
not less.

---

## 3. What the numbers say

All from synthetic players with known true rates. **None of this is evidence
about baseball.**

| Check | Result |
|---|---|
| RMSE vs naive unshrunk rates | **0.00885 vs 0.01857 — 52% better** |
| Recovers generative concentration (900) | k[K] = 917, k[BB] = 1,005, k[OUT] = 705 |
| Prior weight, thin vs thick samples | 0.938 (≈40 PA) vs 0.656 (≈650 PA) |
| Contact table on a barrel (103 mph, 28°) | 53% HR — the synthetic truth for that cell is ≈62% |
| Contact table on a weak grounder (78 mph, −12°) | 76% out |
| Platoon split deviation from overall rate | < 0.05, i.e. heavily shrunk |
| Tests | **214 passing, 2 skipped**, all offline |

The concentration recovery is the result I trust most: the synthetic world was
generated at Dirichlet concentration 900, and the estimator reads back 917 for
strikeouts without being told. That is the machinery working.

**Beating the naive estimator by 52% is not a claim about MLB.** It says the
shrinkage does what shrinkage is supposed to do on data where the answer is
known. The real number will be smaller and is unmeasurable until the backfill
runs.

---

## 4. What I am uncertain about

### 4.1 Attenuation in the contact buckets

The expected-contact transformation compresses the spread of true talent: a
home run contributes ~0.53 to expected HR rather than 1.0. So the estimated
spread is smaller than the real spread, and k comes out **too high** — the
contact buckets are over-regressed relative to optimal.

Visible in the numbers: the generative concentration was 900, and the
discrete buckets recover 705–1,005, while HR comes back at 11,641. That gap is
attenuation, not a different truth.

The direction is conservative (projections too close to league average rather
than too extreme), which is the safe way to be wrong. A proper fix deconvolves
the measurement error. **Unresolved, and the largest known accuracy gap.**

### 4.2 Pooling constants are reasoned, not tuned

`battedball_pooling_k = 40` and `hierarchy_pooling_k = 400` are structural
choices with stated reasoning, not fitted values. The first started at 200 and
was smoothing genuinely distinct cells — a barrelled ball really is nearly all
home runs — back toward their band average. Both should be tuned out-of-sample
once real data exists. I changed one of them after seeing a test fail, which is
exactly the loop this project is supposed to be suspicious of; the reasoning is
in the config next to the value so it can be re-examined.

### 4.3 The proxy has never seen real signal

`fit_proxy_model` is unit-tested against a constructed relationship and
correctly declines to be used when there is none. But the synthetic generator
gives every PA identical pitch counts, so the end-to-end path where the proxy
*is* usable has never run. Expect the first real fit to need attention.

### 4.4 Not yet done

- **Preseason projection prior** — waiting on your sample file.
- **Minor league equivalencies** — deferred by agreement; true rookies get the
  hierarchical prior with wide uncertainty.
- **Park and environment adjustments** — the contact table is league-wide, so a
  Coors fly ball and a Petco fly ball currently get the same expected outcome.
  This is a real omission and belongs in the matchup layer, not here.
- **Times-through-the-order penalty**, umpire and catcher framing effects —
  matchup layer.

---

## 5. What would falsify this

1. **On real data, the contact buckets saturate again.** Would mean the
   sampling-variance correction is still wrong for real batted balls, and the
   projector is inert.
2. **Shrunk projections do not beat naive rates out-of-sample on real seasons.**
   The synthetic result would then be an artefact of the generator.
3. **Fitted k for strikeout rate lands far from the ~60–200 PA range** that
   published stabilisation work implies for real baseball. The synthetic
   recovery of 917 was correct *for a world generated at concentration 900*;
   real baseball has wider talent spread and should produce a much smaller k.
   **If it does not, the estimator is mis-specified on real data.** This is the
   single sharpest check available on the first real run.
4. **Projections correlate with vendor projections below ~0.8** once FanGraphs
   is running alongside. Not proof of error — the whole point is orthogonal
   information — but below that, one of us is wrong and it is probably me.
5. **Park-blind contact estimates show systematic error by venue.** Expected;
   the size of it determines how urgent the matchup layer is.

---

## 6. Honest assessment

The machinery is sound and the two bugs found were both the dangerous kind —
silent, plausible-looking, and fatal to the premise. Finding them on synthetic
data is the argument for having built the synthetic harness first.

What this milestone does not establish: that any of it describes baseball. Zero
real plate appearances have passed through it. The 52% improvement is a
statement about an estimator on data where the answer was written down in
advance.

The three things most likely to be wrong on first contact with real data, in
order: the attenuation gap in §4.1, the proxy fit in §4.3, and park-blindness
in §4.4.

---

## 7. Next

1. **Backfill Statcast 2015–2026**, then `build-pa-outcomes` and check the
   taxonomy coverage report — unrecognised `events` values are named, not
   swept into OUT.
2. **Run `build-projections` for one season and check the fitted k values
   against §5.3.** That is the fastest real signal about whether any of this
   holds.
3. Drop a preseason projection file into `tests/fixtures/` and I will build the
   parser against its actual shape.
4. Park adjustments in the matchup layer, then the simulator.
