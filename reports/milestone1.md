# Milestone 1 — Ingestion and point-in-time warehouse

**Date:** 2026-08-28
**Status:** Code complete. Verified against fixtures. **Not verified against real data** — see Constraint 1.

---

## 1. The binding constraint on this milestone

The build environment had **no network egress to any data source**. The egress
gateway returned `403` to `CONNECT` for every host:

```
statsapi.mlb.com:443        403   baseballsavant.mlb.com:443   403
api.open-meteo.com:443      403   www.fangraphs.com:443        403
api.the-odds-api.com:443    403   api.elections.kalshi.com:443 403
clob.polymarket.com:443     403
```

That is an organisation egress policy, not a transient failure. Consequently:

- **Zero real rows were ingested.** The warehouse ships empty.
- Every parser is exercised against **recorded fixtures** whose shapes mirror
  the documented upstream responses.
- The point-in-time guarantees are proven **structurally** (the reader refuses
  the leaky query) and **behaviourally on fixtures**, not on five seasons of
  real data.

The honest framing: this milestone delivers **the machine that produces a
point-in-time-correct warehouse**, and the automated proof that the machine's
guarantees hold. It does not deliver **the warehouse**. `mlb-edge verify` runs
the identical 58 checks against real data and is the acceptance gate that
closes the gap.

---

## 2. What was built

| Component | Detail |
|---|---|
| Warehouse | DuckDB, **22 tables**, every one carrying `as_of_ts` |
| Raw cache | Append-only, content-hash deduplicated, version-stamped, point-in-time readable |
| Ingesters | MLB StatsAPI (schedule/feed/transactions/teams/venues), Statcast, FanGraphs, Retrosheet, Open-Meteo, umpire ratings, The Odds API, Kalshi, Polymarket |
| Point-in-time layer | `pit.as_of()` with structural quarantines; expanding-window aggregates |
| Integrity | 58 checks, run by both the test suite and `mlb-edge verify` |
| Tests | **106, all offline**, 11 seconds |
| CLI | `init`, `backfill`, `verify`, `status`, `probe`, `reload-cache`, `odds poll/close`, `budget` |
| Lint | `ruff` clean |

Roughly 6,500 lines of source, 2,250 of tests.

### Decisions taken from your answers

- **Tier 0 odds** — free plan, moneyline only, forward archive. A 500
  request/month budget is enforced in code against the raw manifest, so it
  cannot drift after a crash.
- **Kalshi + Polymarket as execution venues.** Books are ingested as *signal*
  only. `books.yaml` gives recreational books a consensus weight of zero, and
  `config.py` raises if anyone edits that to non-zero.
- **2021–2025.** 2020 is excluded: 60 games, no fans, seven-inning
  doubleheaders, a different extra-innings rule.
- **Build here, load there.** Hence the `backfill`/`verify` split.

### Ground rules, and where each is enforced

| Ground rule | Enforcement |
|---|---|
| 1. Point-in-time features | `pit.as_of()` requires an explicit timestamp; no "current state" overload exists |
| 2. No random splits | Walk-forward only; expanding-window helper excludes the day being predicted |
| 3. Odds snapshots timestamped; close quarantined | `closing_lines` is a physically separate table, unreachable from `pit.as_of()` at any flag |
| 4. `game_pk` keying | Schema contract test rejects any table keyed on team names; `GameMatcher` refuses ambiguous doubleheaders |
| 5. Devig before comparing | `market/prices.py` deliberately contains conversions only; the stored field is named `implied_prob_raw` |
| 6. Reproducible artifacts | `reload_from_cache(as_of=...)` rebuilds the warehouse from bytes, as of a past date |

The two guards worth calling out, because they turn a discipline into a
property of the code:

- Reading `closing_lines` through the feature reader raises `LeakError`. There
  is no override. Only `pit.closing_lines_for_clv()` reaches it.
- Reading an `OUTCOME` table requires `allow_outcomes=True`. The flag changes
  nothing except making every read of the labels greppable in review.

---

## 3. What the numbers say

Everything below is from fixtures. None of it is evidence about baseball.

- **106/106 tests pass**, no network.
- **58/58 integrity checks pass** on a fixture-loaded warehouse.
- **9/9 Retrosheet plays** parse cleanly, including the three cases that break
  naive parsers: a 6-4-3 double play where the forced runner is named only
  inside the basic play (`64(1)3/GDP`), a walk forcing a chain of runners along,
  and `2XH(9E2)` — a runner marked out at home whose out is negated by the error
  in the annotation. Getting that last one backwards would inflate the out rate
  on exactly the plays where extra bases are taken.
- **Doubleheader keying**: the fixture schedule contains two `game_pk`s sharing
  a date and both teams. The integrity check counts the collision, the odds
  parser resolves both games to different `game_pk`s by commence time, and the
  matcher **refuses** when two candidate starts fall within 90 minutes rather
  than picking one.
- **Statcast revision**: a restated pitch is read as 94.2 mph at a 2025 as-of
  and 95.8 mph at a 2026 as-of, from the same warehouse.
- **Leak detection**: shifting every lineup's `as_of_ts` forward one day empties
  a pre-game read. If it had not, the as-of filter would not be binding.

---

## 4. What I am uncertain about

### 4.1 Historical projections may not be obtainable at all — the largest risk here

FanGraphs serves *current* projections. There is no endpoint for "what did
Steamer say on 12 May 2023". The ingester therefore stamps every snapshot with
today's date and refuses to backfill, because writing today's projection under a
2023 date would be the purest form of lookahead.

The consequence is structural and worth confronting now rather than at
Milestone 4: **a 2021–2025 walk-forward backtest has no projection-based
true-talent base.** The daily snapshots only start accumulating from the first
run. Options:

1. **Build an in-house Marcel-style projector** from prior-season Retrosheet and
   Statcast data with expanding windows. Weaker than ZiPS or Steamer, but
   backfillable, point-in-time clean by construction, and fully reproducible.
   **This is my recommendation** — a slightly worse projection you can honestly
   backtest beats a better one you cannot.
2. Backtest only from the day snapshots begin. Clean, but pushes real evidence
   a year out.
3. Source a historical projection archive. I could not verify one exists at a
   sane price.

Doing (1) and (2) in parallel is coherent: Marcel for the historical
walk-forward, FanGraphs for live operation, and the gap between them becomes a
measurable quantity rather than an assumption.

### 4.2 Three response shapes are unverified

FanGraphs, Kalshi and Polymarket parsers are driven by config field maps and
regexes because I could not see a real response. They are written to produce
**nulls rather than exceptions** on a wrong map, which means a bad map degrades
quietly — so `verify` measures the null rate and `probe` dumps observed keys.
Expect to spend an hour correcting these on first live run. `reload-cache`
reparses without re-fetching.

### 4.3 Deliberate gaps, left null rather than guessed

- **Park orientations.** `cf_bearing_deg` is null for all 39 park entries. Resolving a
  wind direction into "blowing out to centre" needs it, and I would have been
  guessing. A wrong bearing silently reverses the sign of a real effect at half
  the parks, which is worse than no wind feature. `wind_components()` returns
  `None` without it. **Blocks the wind feature entirely** — and Part 8 rates
  weather-driven repricing as the most reliable edge available, so this is on
  the critical path, not a nicety.
- **Umpire run effects.** `called_strike_rate_oe` is computed properly
  (batter-specific zone normalisation, expanding window, shrunk toward league
  average). `k_pct_delta`, `bb_pct_delta` and `runs_per_game_delta` are null:
  converting a called-strike bias into a run effect needs a count-transition
  run-value model, and a plausible-looking constant would propagate into every
  price.
- **Retrosheet → `game_pk` crosswalk.** Not built. Transition matrices are
  estimated from base-out states and do not need it, but it is a real hole.

### 4.4 Things I believe but have not tested

- Statcast's 30,000-row cap triggers adaptive splitting. The detection logic is
  unit-tested; the recursion has never run against a real truncated payload.
- Rate limits in config are guesses at politeness, not measured limits.
- Retrosheet parse coverage is 100% on nine fixture plays. Real files contain
  rundowns, obstruction and interference. **Expect the first real run to land
  below the 99.5% threshold**, and treat that as the parser telling you what it
  does not yet handle.

---

## 5. What would falsify this approach

Concrete, checkable, in rough order of how much each would hurt:

1. **`mlb-edge verify` reports errors on the real backfill.** Any ERROR means
   the warehouse is not safe to model on. This is the acceptance gate.
2. **Retrosheet parse coverage stays below ~99% after a round of fixes.** The
   advancement matrices would carry an unknown bias, and Part 3.3's
   "empirical advancement matrices, not simplified assumptions" would be
   unmet.
3. **Projection MLBAM resolution below 95%.** Means the field map is wrong and
   the projection set silently covers part of the league.
4. **Unresolved market events above a few percent.** The `game_pk` join is the
   most dangerous one in the system; a high refusal rate means CLV would be
   computed on a biased subsample of games.
5. **Statcast revisions turn out to be large, not cosmetic.** If re-pulling
   2023 materially changes 2023 xwOBA, then any model fit on restated data is
   not the model you could have run — and the `--as-of` rebuild path becomes
   mandatory rather than a nicety.
6. **The Tier 0 archive proves too thin.** One poll/hour on 500 requests/month
   is roughly 16 polls/day. If line movement turns out to need finer resolution
   to price, Tier 0 is not a slow path to CLV evidence but a dead end, and the
   Tier 1 spend moves up.

---

## 6. Honest assessment

The riskiest thing about this milestone is that it **looks** finished. 106
green tests and 58 green checks are a real result about the *code*, and no
result at all about *baseball*. Not one row of real data has passed through
this system.

What I am confident in: the structure. The leak-prone queries raise instead of
returning; the raw cache genuinely reconstructs the past; `game_pk` keying and
doubleheader refusal are enforced rather than intended.

What I am not confident in: every place where I had to write down a fact about
the world without being able to check it. Response field names, ticker formats,
rate limits, park altitudes. Those are all *soft* failures by construction —
nulls and refusals, not silent wrong numbers — but there will be a list of them
after the first live run, and it will be longer than I would like.

The single most important thing to do next is not Milestone 2. It is to run
`backfill` and `verify` on a machine with network access, and to treat the first
`verify` output as the actual Milestone 1 result. Everything in §4 is a
hypothesis until then.

---

## 7. Next

1. **Run the backfill** on the DigitalOcean box; treat `verify` output as the
   real deliverable. Fix field maps via `probe` + `reload-cache`.
2. **Start the odds poller immediately**, before any modelling. The CLV dataset
   is the long pole in the whole mission and it only accumulates in wall-clock
   time. Every day not polling is a day added to the end.
3. **Decide the historical-projection question** (§4.1). It gates Milestone 2's
   true-talent rates.
4. **Derive park orientations** to unblock the wind feature.
5. Then Milestone 2: the simulator.
