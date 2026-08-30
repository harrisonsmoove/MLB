# mlb-edge: the plan

Written so neither of us re-derives it. Everything here is settled unless
explicitly marked open. If a future session proposes something that contradicts
this document, the contradiction is the finding — resolve it here first.

Last revised: 2026-08-30.

---

## 0. Where this stands

**Frozen.** No engineering starts until `mlb-edge poll-status` shows rows
accumulating on the box. The poller is the only irrecoverable item on the
project: Tier 0 of The Odds API has no historical endpoint, so an hour not
polled is an hour that does not exist, ever. Everything else in this document
can be built in January as easily as today.

The freeze has exactly two carve-outs, both now done: this document, and the
season-conditional rules fix at `config/settings.yaml:492`.

---

## 1. Timeline: this season is not the evaluation

Roughly 350-400 regular season games remain, plus the postseason. Even with a
perfect poller from today that does not reach the 300-500 qualifying-bet
threshold that Milestone 5 needs to say anything about closing line value.

So:

| Season | What it is for |
|---|---|
| 2026 (remainder) | Pipeline validation. Starting the odds archive. |
| 2027 | The actual evaluation. |

The consequence, and it is the reason this section is first: **do not compress
the modelling to try to bet this September.** There is no prize for a rushed
simulator in the last four weeks of a season whose bets cannot be evaluated. The
prize is an odds archive that starts today and a model that is correct in April.

What this season is genuinely for:

1. **The archive.** Every hour polled is a permanent asset.
2. **Falsifying the pipeline end to end** — ingest, PIT, devig, price, log —
   against live quotes, in paper mode, where a bug costs nothing.
3. **The two gates below.** Both can be passed on historical data. Neither needs
   a single 2026 bet.

Paper-trade the remainder of 2026 anyway. Not for the CLV number, which will not
be significant, but because a pipeline that has never seen a live slate has
never been tested.

---

## 2. Standing rules

These apply to everything below, without further mention.

**Silent fallbacks get a voice.** Any fallback, default, or degraded path
announces itself in the logs at WARN and is pinned by a test. If it can be
silently wrong, it gets a voice. This rule exists because three separate bugs in
one session had the identical shape — code takes a degraded path and reports
success:

- the Kalshi cursor was never followed, truncating the live board while logging
  `errors=0`;
- `season_end_date` was nested under the wrong config block, so the budget paced
  against a guessed horizon;
- the projector fell back to a default constant without saying so.

`errors=0` is not evidence of anything. The completeness check exists because of
this rule, and so does every WARN in `rules.py`.

**Pre-register, then look.** Any number that decides whether we continue is
written into the repo, with its derivation, before the run that produces the
comparison. A number you look at and then decide whether you like is not a test.

**Point-in-time or it did not happen.** Every table carries `as_of_ts`. Feature
reads are bounded by decision time. Closing lines live in a physically separate
table that the feature reader cannot reach at any flag setting. Label reads
require `allow_outcomes=True` so they are greppable.

**`game_pk` is the key.** Never `(date, home_team, away_team)` — it collides on
doubleheaders. `GameMatcher` refuses an ambiguous doubleheader rather than
guessing.

**Baseball only.** No other sports.

---

## 3. The sequence

Nothing below starts until poll-status is green.

```
a. Backfill 2018-2019, --sources teams,venues,schedule,statcast
b. PA extraction, watch coverage=
c. Projections one season -> k gate       <- STOP HERE, report the number
d. (only if gate passes) full 2015-2026 backfill incl. game feeds
e. Retrosheet parse, fix event codes not thresholds
f. Park bearings -- measured by hand, test already exists
```

Two notes on why it is ordered this way.

**(c) sits in front of (d) deliberately.** The full backfill is roughly eight
hours. Putting the first falsification test behind it means paying eight hours
to find out whether the thing is worth eight hours. Steps (a)-(c) need only four
sources and one season's projections.

**(e) fixes event codes, not thresholds.** Retrosheet coverage will be under
99.5% on first contact. The fix is to add the missing event codes. Lowering the
threshold to make it pass is prohibited — it is the exact move this whole
document exists to prevent.

Same for the FanGraphs and Kalshi field maps: probe, reload the cache, and
report what changed. Do not silently accommodate a schema drift.

---

## 4. Gate 1 — the regression constant `k`

**Nothing is written in `model/` until both conditions hold, in this order:**

1. `mlb-edge verify` runs clean — zero ERRORs.
2. The fitted `k` passes the pre-registered ratio check.

This is enforced in code (`src/mlb_edge/gate.py`), not by discipline. The gate
record is bound to a fingerprint of the warehouse inputs and expires when they
change, so a stale pass cannot authorise new work.

### The target

Pre-registered in `src/mlb_edge/features/preregistration.py`:

```
k = mu(1-mu)/sigma^2 - 1
  = 0.22 * 0.78 / 0.055^2 - 1
  = 55.7          <- strikeout rate, HITTERS
```

with walk rate at 123.4 for information, and

```
RATIO_BOUNDS = (0.5, 2.0)
```

Gated bucket: batter strikeout rate, fitted on the >=300 PA population. Batters,
not pitchers — the target is derived from the spread of *hitter* talent and does
not transfer to a pitcher fit. The >=300 PA fit rather than the >=200 one so the
population is comparable to the one the target's assumptions describe. Both
thresholds are fitted and both reported; only one gates.

### Two identities, kept straight

```
reliability(n) = n / (n + k)          -> equals 0.5 exactly at n = k
sigma^2_true   = mu(1-mu) / (k + 1)   -> the +1 lives here, and only here
```

The published "K% stabilises around 60 PA" figure is **not** independent
corroboration of 55.7. The stabilisation point *is* `k`, and the published
figure is obtained by decomposing observed variance into true and binomial
parts — the second identity rearranged. It is the same relationship evaluated
from a different published input. Agreement means the inputs are mutually
consistent; it is not evidence either is right.

**Read 55.7 as "somewhere in the tens", not as a measurement.** What actually
protects us is the width of the ratio band. A factor of two is generous
precisely because the target is one estimate with one set of assumptions.

### What failing means

A ratio outside [0.5, 2.0] stops the project at step (c). It does not trigger a
search for a different bucket to gate on, a different population filter, or a
softer band. The estimator is wrong and the finding is that the estimator is
wrong.

Contact-derived buckets (1B, 2B, 3B, HR, OUT) are deliberately **not** gated.
Their constants are known to be biased upward by the expected-contact
transformation, which attenuates the true-talent spread. Gating on a number we
already expect to be wrong in a known direction would be theatre.

---

## 5. The simulator

Does not start until (c) reports a passing ratio.

### (i) Base-out state machine

Discrete PA outcomes in, runs out. Retrosheet advancement matrices. No pitchers,
no fatigue, no environment.

Included as **structure, not realism**:

- the home half of the 9th is not played when the home team leads;
- extra innings;
- the automatic runner on second, **season-conditional**.

That last one is now `src/mlb_edge/rules.py` rather than a boolean in the
config. An unconditional flag silently applied the 2020 rule to 2018-2019, which
compresses the extra-innings tail — and the tail is precisely what (ii)
measures. The same module carries the designated hitter, which moves league run
scoring by roughly 0.2-0.3 R/G and would otherwise be attributed to the state
machine:

| Rule | Encoded |
|---|---|
| Runner on second | Regular season 2020+; never postseason |
| DH, AL | 1973+ |
| DH, NL | none through 2019; 2020 only; none 2021; universal 2022+ |

A season outside 2015-2026 warns rather than defaulting.

### (ii) Validate against a known run environment — HARD STOP

Before anything is added. The validation **replays the actual season's games
with actual lineups and starters**, not a synthetic average lineup. That
sidesteps the average-lineup problem and the DH-era problem entirely.

If it cannot reproduce a season it has already seen, it cannot price a game it
has not. Bands are pre-registered in section 6.

### (iii) Starter hook + times-through-order

Fit the hook from Retrosheet rather than assuming a pitch-count rule. Re-run
(ii) after: **a regression check.** Adding realism should not break the
aggregate it already matched.

### (iv) Bullpen sequencing and availability

From recent usage logs. Re-run (ii): **a regression check.**

### (v) Environment layer

Park components, weather, umpire, framing. Applied in log-odds space before
renormalising.

Re-running (ii) here is **a park-factor CENTERING check, not a regression
check.** Labelled that way on purpose. The park layer redistributes run scoring
across venues; it must not move the league aggregate. If the aggregate shifts,
the component factors are not centred — that is a centring bug, not evidence
that the simulator regressed, and it must not be read as one.

Its own separate test, independent of (ii): **the component factors normalise to
1.0 league-wide.** Asserted directly, not inferred from the aggregate matching.

Rule at every step: each addition gets re-validated. If the aggregate drifts,
the addition is wrong.

---

## 6. Pre-registered bands for (ii)

Written before the first run, same discipline as `k`.

### How the tolerances were derived

The simulator, given enough sims, is effectively noiseless. So the band is not
set by simulator noise — it is set by the **sampling error of a single season**.
One season is 2430 games, so:

```
N = 2430 games x 2 teams = 4,860 team-games
```

| Quantity | Value | SE | 3 SE |
|---|---|---|---|
| Shutout rate | p ~ 0.075 | 0.378 pp | 1.13 pp |
| P(10+ runs) | p ~ 0.060 | 0.341 pp | 1.02 pp |
| Mean R/team-game | mu ~ 4.2, sd ~ 3.1 | 0.0445 | 0.133 |
| Home/away run correlation | n = 2430 games | 0.0203 | 0.061 |

Total variation distance, noiseless model against one season of draws over bins
`0,1,...,9,10+` (20,000 multinomial replications):

```
p50 = 0.0174    p95 = 0.0250    p99 = 0.0284    p99.9 = 0.0321
```

Note the comparison being made. The **two-sample** TVD floor — two independent
seasons against each other — is higher, p95 = 0.0354. That is not our
comparison: the model side carries no sampling error. Using the two-sample floor
would have handed us a band 40% too wide.

### The bands

| Check | Statistic | Band | Basis |
|---|---|---|---|
| Mean runs | \|Δ mean R/team-game\| | ≤ **0.13** | 2.9 SE |
| Mean runs, home | \|Δ mean R/game, home only\| | ≤ **0.13** | 2.9 SE |
| Mean runs, away | \|Δ mean R/game, away only\| | ≤ **0.13** | 2.9 SE |
| Shutout rate | \|Δ P(0 runs)\| | ≤ **1.15 pp** | 3.0 SE |
| Big games | \|Δ P(10+ runs)\| | ≤ **1.05 pp** | 3.1 SE |
| Distribution shape | TVD over bins 0..9,10+ | ≤ **0.032** | p99.9 of null |
| Home/away correlation | \|Δ rho\| | ≤ **0.06** | 3.0 SE |

**Split the mean by home and away, do not pool it.** A simulator that wrongly
plays the home half of the 9th adds roughly 0.2 R/game to the home side and
nothing to the away side. Pooled, that is 0.10 — inside the band. Split, the
home row fails cleanly. This is the diagnostic that actually catches the bug the
correlation check was reaching for.

### The targets

The **tolerances above are fixed now** — they follow from sample size and
nothing else. The **targets** (the season's actual shutout rate, P(10+), mean,
distribution, correlation) are measured from the season data and written into
the repo *before the first simulator run*. Measuring them involves no model
output, so there is no leak; both the number and the threshold are in the repo
before any output is seen, as required.

### Chi-square is reported, TVD gates

The formal chi-square (11 bins, df = 10; critical values 18.31 / 23.21 / 29.59
at p95 / p99 / p99.9) is **reported with its p-value but does not gate**, and
the reason is stated here in advance so it cannot look like a convenient choice
later: at N = 4,860 the test has the power to reject on bin differences of a few
tenths of a percentage point, which are irrelevant to pricing a total. It
answers "is the simulator exactly right?" — the answer is always no, and here we
can prove it.

TVD is an effect size. It bounds total probability misallocation, which maps
directly onto pricing error. It gates.

A chi-square rejection is a flag to inspect *which* bins moved, not an automatic
stop — with one exception, so the reported statistic is not toothless: **chi-square
above 100 is a hard stop regardless of TVD.**

### The one permitted appeal

The TVD null above assumes iid multinomial team-games. Real team-games are
neither: teams are heterogeneous, and the two teams in one game share park,
weather, and umpire. Both push the effective N below 4,860, which makes the band
**tight**, not loose. A marginal failure is therefore more likely a false alarm
than a false pass.

So one appeal is permitted, and only this one: re-derive the null by **block
bootstrap over whole games** from the actual season, which preserves both the
heterogeneity and the within-game pairing, and take the band as the p99.9 of
that null. This is computed from season data alone — no model output enters it —
so it is computed and written into the repo **before the first simulator run**,
alongside the targets. The iid figure 0.0321 is recorded here now so that any
movement is visible.

No other appeal. If the block-bootstrap band is also exceeded, (ii) has failed.

### On the home/away correlation sign

Flagging a disagreement rather than encoding it. The expectation of "near zero
and slightly negative" mixes at least three effects pointing in different
directions:

- the unplayed home half of the 9th removes home runs precisely in games where
  the home team is already ahead, which strips anti-correlated mass and pushes
  rho **up**;
- shared park, weather, and umpire push **up**;
- a trailing team emptying its low-leverage bullpen lets the leader score more,
  pushing **down**.

The net sign is a question for the data, not for either of us. **Measure it, do
not assume it.** The diagnostic is unaffected: it compares the simulator against
the same season's measured rho, so it works whatever the sign turns out to be.
And per the note above, the split home/away means are the sharper test for the
specific bug — an unplayed-half-inning error will fail that row by a wide margin
regardless of which way rho leans.

---

## 7. Gate 2 — the model-market blend

Calibration and the blend come after all five simulator steps.

The fitted `w` in the model-market blend is the second real falsification point.
Expect it low. **Near zero means the simulator adds nothing and we stop rather
than tune.**

### `w` is fit per market type, not once

Pre-registered now, before any fit exists:

| Market | Role |
|---|---|
| F5 moneyline | **Decides whether we continue** |
| F5 total | **Decides whether we continue** |
| Pitcher strikeout props | **Decides whether we continue** |
| Pinnacle full-game moneyline | Reported for completeness. **Not a stop condition.** |

Fitting `w` once against Pinnacle full-game moneyline and stopping on `w ~ 0`
would mean quitting over failure in the single market we already decided not to
compete in. Near-zero there is the *expected* result, not a negative one.
Pinnacle's full-game moneyline is the most efficient price in the sport; a
bottom-up simulator has no business beating it and its failure to do so says
nothing about the F5 or prop markets, where the edge is supposed to live.

The three markets that decide are the ones where a bottom-up PA-level simulator
has structural reason to know something the market's top-down price does not:
the first five innings are the starter plus the top of the order, which is
exactly what the model resolves; strikeout props are a direct read of the PA
outcome distribution.

---

## 8. What "stop" means

Three stop conditions, in order of when they can fire:

1. **`k` outside [0.5, 2.0] × 55.7** — stop at step (c), before the expensive
   backfill.
2. **(ii) outside the bands in section 6** — stop before adding a single line of
   realism to the simulator.
3. **`w` near zero across F5 ML, F5 total, and K props** — stop before betting.

"Stop" means stop. It does not mean widen the band, change the gated bucket,
switch the population filter, or fit a different market. Each of these gates
exists because it is the cheapest available opportunity to find out the thing
does not work.

The poller keeps running through all three. If the model stops, the archive is
still worth having, and it is the one asset that cannot be rebuilt later.
