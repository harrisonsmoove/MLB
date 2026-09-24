# Adverse selection: whose edge is the gap?

Pre-registered 2026-09-24, before any gap has been measured. Uses only the 25
days already archived. Costs nothing and answers a question that makes stage
one meaningless if it comes back wrong.

---

## 1. The question

When Kalshi disagrees with the sharp consensus, two explanations produce an
identical gap distribution:

* **Kalshi is stale.** The sharp price moved, Kalshi has not caught up, and the
  gap is ours to take.
* **Kalshi is informed.** Someone there knows something the sharp books have
  not priced — a scratch, a bullpen note, a weather read — and the gap is
  theirs.

No amount of gap frequency separates these. A board with a 40% hit rate of
large gaps looks equally good under both, and is a business under one and a
slow bleed under the other. On a thin board the second is common, because thin
boards are where informed flow moves the price furthest.

---

## 2. The test

For every historical gap above `floor(P)`, measure what the Kalshi price did
next. **Toward** the sharp price means Kalshi was stale and the gap was real.
**Through** it means we were the stale ones.

This is a closing-line-value measurement wearing different clothes, and that is
the point: CLV against Pinnacle's close is the project's stated success
criterion, and this asks the same question of Kalshi's own close on data
already held.

### Horizons, all reported, one gating

| Horizon | What it answers |
|---|---|
| +1 Kalshi snapshot | immediate reversion — does it start moving at all |
| +4 snapshots (~1 h) | whether it persists long enough to trade |
| **last quote before first pitch** | **the gate.** Kalshi's own closing price |

The closing horizon gates because it is the one that corresponds to holding the
position, which is the simplest strategy and the one with no execution risk in
the measurement.

---

## 3. Why 50% is the wrong threshold

The proposal on the table was: *if gaps resolve against me at or above 50%, the
strategy is dead.* The first half is right — below 50% it is definitively dead.
The second half is where it fails: **above 50% proves nothing, because 50% is
break-even only when friction is zero.**

Buy at `K` when the sharp price says `S`, gap `g = S − K`, hold to settlement.
A fraction `f` of the time the sharp price was right and we gain about `g`.
The rest of the time Kalshi was right, and what we lose depends on why:

```
EV = g·(2f − 1) − friction        if "through" means true value is past K
EV = g·f − friction               if "through" means the gap was simply noise
```

Break-even `f`, at the 2.75 pp friction floor for a coin-flip contract:

| Gap `g` | break-even `f` (adverse) | break-even `f` (noise) |
|---|---|---|
| 2 pp | **impossible** | **impossible** |
| 3 pp | 95.8% | 91.7% |
| 4 pp | 84.4% | 68.8% |
| 5 pp | 77.5% | 55.0% |
| 7 pp | 69.6% | 39.3% |
| 10 pp | 63.7% | 27.5% |
| 15 pp | 59.2% | 18.3% |

**A 60% resolve-toward rate on 3 pp gaps is a losing strategy**, comfortably. A
threshold of 50% would have passed it.

The two columns bracket reality — genuine adverse selection sits nearer the
left, pure noise nearer the right — and the gap between them is wide enough
that **picking either would be choosing an answer.** So the gate does not use
a win rate at all.

---

## 4. The criterion, pre-registered

> **Primary.** For gaps above `floor(P)`, the **mean realised convergence** —
> the signed movement of the Kalshi price toward the sharp price, measured to
> the last quote before first pitch — must exceed `floor(P)`.
>
> Stated per gap-size bucket and pooled. The pooled figure gates.

Magnitudes, not a win rate. The archive holds how far each gap moved, so the
model-dependence in section 3 is avoidable: measure the thing the models were
trying to approximate. A strategy that wins 55% of the time but wins big and
loses small is fine, and a win-rate test would kill it; one that wins 80% of
the time in 1 pp increments and loses 8 pp when wrong is not, and a win-rate
test would pass it.

**Reported alongside, not gating:**

* resolve-toward **rate** per gap bucket, plotted against the break-even curve
  above, so a rate is never read without the threshold it has to clear;
* the **distribution** of realised convergence, not just its mean — a mean
  carried by three outliers is a different finding from a mean carried by the
  body;
* the same figures split by **time to first pitch**, because lineup and weather
  news arrives on a schedule and informed flow with it.

### The hard stop

> If mean realised convergence is **negative** at any gap size above the floor,
> the prediction-market-versus-sharp-book trade is dead and stage one is not
> run. A negative figure means the archive says we are the slow side.

This is the one result that ends it outright rather than resizing it. It costs
nothing to find out and it invalidates everything downstream, which is why it
runs before stage one rather than after.

---

## 5. What a pass licenses

The same restraint as the stage-one gate: **passing is not permission to
trade.** It licenses running stage one at all. Convergence exceeding friction
on historical gaps says the signal is not backwards; it does not say gaps occur
often enough, or that they survive execution, or that they persist below the
15-minute cadence we can currently see.

---

## 6. Known limits of this study

Stated now so they are not discovered as excuses later.

* **The archive's sharp leg was polled at ~19 minutes, not ~1.** Everything
  measured here inherits the alignment error in `stage-one-gate.md` §3a, which
  is larger than it will be going forward. That biases against finding
  convergence, so a pass is trustworthy and a marginal fail is not.
* **Orderbook depth was capped at 10 levels for these 25 days.** Irrelevant
  here — this study uses prices, not depth — but it means the same window
  cannot also answer the size question.
* **25 days is roughly 350 team-games**, and gaps above the floor will be a
  fraction of those. If the qualifying count is under 50 the study is
  underpowered, and the honest output is "not enough gaps yet", not a verdict.
  That number gets reported first, before any convergence figure.

---

# The 2026-09-24 run was a bug, not a finding

The first run reported 303,372 qualifying gaps out of 450,115 paired quotes —
67% of all observations clearing a 2.3pp floor, mean gap ~20pp — and a verdict
that passed on the strength of a single bucket. **That result is withdrawn.**
Two independent defects produced it. Both are fixed; both are now pinned by
tests that fail if either is reintroduced.

## Defect 1: side inversion

`parse_ticker` captured the ticker's side suffix (`...TORBAL-BAL`) in its
regex and then discarded it — `ParsedTicker` had no field for it and nothing
read it. The study stored each Kalshi mid under `_pair_key(*sorted(teams))`,
an **unordered** key that by construction throws away which team the price is
the probability of. The sharp leg stored `probs[0]`, which is always the
**home** team.

So whenever a ticker's YES side was the away team, the study compared
`P(away)` against `P(home)`. The error is exactly `1 − 2p`: on a 60/40 game,
20 points. Large, plausible, and in the direction that looks like free money.

Predicted signature versus what the run reported:

| | predicted under full inversion | observed | fixed |
|---|---|---|---|
| mean gap | 13pp | ~20pp | — |
| share clearing a 2.3pp floor | 91% | 67% | — |

The observed numbers sit between "no inversion" and "total inversion", which
is what a *mixture* looks like: roughly half the tickers name the home team
and were fine, half name the away team and were backwards.

**Fix.** `ParsedTicker` now carries `side_code` and resolves `side_team`
through the same alias table as the matchup codes. `Quote` carries the team its
price refers to. Every comparison goes through `orient()`, which restates one
price as the probability of the other's team and returns `None` — a refusal,
counted and reported — when either side is unknown. An unresolvable side is
dropped, never guessed: half of those guesses would be backwards.

## Defect 2: in-play contamination — and the answer to the n drop

You asked why n fell from 302,793 at +1 snapshot to 97,061 at close. It is
not sampling; it is the same defect twice.

The study never filtered to pre-game quotes. The Odds API tier-0 h2h feed is
**pre-match only**: its last quote stands frozen at first pitch. Kalshi keeps
trading through all nine innings. So every in-play Kalshi snapshot was being
differenced against a stale pre-game number, and as the game moves toward a
result that difference widens without bound — which is a second, independent
source of the implausibly large gaps.

The n drop falls straight out of it. `attach_outcomes` defines the close as the
last Kalshi quote *at or before* first pitch, and only assigns it when
`closing_at > gap.at`. A gap observed after first pitch therefore has no close
by construction:

```
302,793  gaps with a +1 snapshot horizon
 97,061  gaps with a close
-------
205,732  gaps that occurred after first pitch  (68%)
```

Two thirds of the sample was in-play. The close row was the only row that
excluded it — which is why the close horizon disagreed with the others, and
why the exclusion was invisible rather than reported.

**Fix.** `find_gaps(pregame_only=True)` is the default; in-play quotes are
excluded and counted. `--include-in-play` restores the old behaviour
deliberately. The run now prints every exclusion reason before any verdict.

## Defect 3 (latent): no sanity bound

Neither defect had to survive to a verdict. Both produce gaps far outside what
two venues pricing the same baseball game can disagree by, and nothing was
checking.

`MAX_PLAUSIBLE_GAP = 0.25`. MLB moneylines live roughly between 0.25 and 0.80;
a 25-point disagreement between two venues on the same game is not a
disagreement, it is a comparison error. Gaps above the bound are **excluded and
reported in red**, with the count and the instruction to treat it as a bug
report. `--max-gap 0` disables it deliberately. The gap-size table's top bucket
is now labelled to the bound rather than 10–100pp, since nothing above it can
be present.

## What to run

```bash
mlb-edge adverse-selection --dump 20 --dump-min 0.10
```

`--dump` prints individual records end to end — timestamp and minutes to first
pitch, matchup, ticker, resolved YES side, best bid on each side of the book
and the implied ask, the mid and whose probability it is, the raw American
prices, all four devig methods, the oriented comparison, the gap against its
floor, and the close with its convergence. One case readable top to bottom,
rather than an aggregate that has to be trusted.

## What this cost, and the rule it argues for

Three defects, and the one that mattered was not in the arithmetic.
`find_gaps` was correct the whole time on quotes that carried their team. The
inversion lived in the wiring between the archive and the arithmetic, in a key
that was *designed* to discard orientation — `sorted(teams)` was deliberate,
and correct for its original purpose of matching a matchup. It was reused for
something that needed the thing it throws away.

The unit tests could not see it because they constructed quotes directly.
`tests/test_adverse_cli.py` now drives the whole path from a synthetic
two-game archive, and seven tests across the two files fail if the inversion
is reintroduced (verified by putting it back).

Proposed as a third standing rule:

> **Any price carries what it is the probability of.** A bare number with two
> possible referents will eventually be read against the wrong one, and on a
> binary market the error is `1 − 2p` — largest exactly where the money is.

---

# Correction, same day: the n-drop explanation above is wrong

The section above attributes the drop from 302,793 to 97,061 to in-play
contamination. **That explanation does not survive checking, and the premise
under it was never measured.** Both are corrected here rather than edited
away, because the reasoning error is the useful part.

## The arithmetic that breaks it

`find_gaps` pairs each Kalshi quote with the most recent sharp quote at or
before it, and drops the pair when that quote is more than 30 minutes old. If
the sharp feed really were pre-match only, its last quote for a game would
stand at first pitch and the staleness filter would cut every Kalshi quote
more than 30 minutes into the game. A baseball game is about three hours. So
pre-match-only feed plus 30-minute staleness bounds in-play contamination at
roughly a sixth of a game — nowhere near two thirds of the sample.

The two claims are inconsistent with each other. I did not notice because each
one sounded right on its own.

## Defect 3: a matchup is not a game

The real mechanism. Both legs were keyed on `_pair_key`, built from
`sorted({canonical(home), canonical(away)})` — **no date, no start time**.
Toronto and Baltimore meet three or four times in a series and a dozen times a
season. Every one of those meetings collapsed onto one key:

```
KXMLBGAME-26SEP231905TORBAL-BAL  ->  Baltimore Orioles|Toronto Blue Jays
KXMLBGAME-26SEP301905TORBAL-BAL  ->  Baltimore Orioles|Toronto Blue Jays
                                     same key
```

One merged price series per team pair, with **one** first pitch and **one**
close standing for every meeting in the archive. And `first_pitch` was
populated with `if raw and pair not in first_pitch` — the *earliest* game
seen for that pair.

The n drop falls straight out. The close is the last Kalshi quote at or before
that earliest first pitch, and is assigned only when `closing_at > gap.at`. So
**only gaps occurring before the first meeting's first pitch got a close.**
Everything from the second meeting onward — days or weeks of quotes — got
none. For a pair with markets opening a day or so ahead and three or four
meetings inside the archive, the share of quotes falling before that first
first-pitch lands around a quarter to a third. Observed: 97,061/302,793 = 32%.

This is the `game_pk` rule one level down. The rule was already written for
doubleheaders — never key on `(date, home, away)` — and this key does not even
carry the date.

`_pair_key` was not a bug. It was correct for grouping a matchup, which is
what it was written for. It was reused for a game-by-game join, which needs
the exact thing it discards. Same shape as the side inversion: the second
reuse of a correct abstraction, in a place that needed what it threw away.

**Fix.** The study now joins game to game. The sharp leg keys on
`pair|commence_time`; the Kalshi leg keys on `ParsedTicker.event_ticker`,
which carries the date and start time. The two are joined by `join_tickers`,
which was already written to separate doubleheaders by inferring the ticker
clock offset, and which refuses the ones it cannot tell apart. The run now
prints games joined, games refused as ambiguous, and games the board does not
list.

## What is still unverified

Whether the feed carries in-play prices at all is **not established**. I
asserted it. It is measurable from the archive already on disk:

```bash
mlb-edge probe-inplay
```

See `reports/odds-feed-pricing.md` for what each outcome implies. The
adverse-selection run also prints `sharp quotes observed AFTER first pitch`
before any verdict, so the fact is visible on every run.

The pre-game filter stays on regardless. It is correct whether or not the feed
carries in-play, because differencing a Kalshi in-play price against a sharp
quote of unknown freshness is not a measurement of anything — and with the
exclusions now printed, its cost is visible rather than silent.

## Three defects, one shape

| | What was reused | What it discarded |
|---|---|---|
| Side inversion | `sorted(teams)` as a price key | which team the price is *of* |
| Matchup collision | `sorted(teams)` as a game key | which *meeting* it is |
| In-play | — | whether the reference was still live |

The first two are the same line of code, reused twice for jobs that each
needed one of the two things it drops. The standing rule now in the README
covers the first. The second is the `game_pk` rule, which already existed and
which I did not apply here.
