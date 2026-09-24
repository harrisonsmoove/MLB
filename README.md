# mlb-edge

MLB game simulation and betting system. The success criterion is **closing line
value against Pinnacle's no-vig close**, not backtested ROI.

Current state: **Milestone 1 (ingestion + warehouse + point-in-time integrity).**
See [`reports/milestone1.md`](reports/milestone1.md) for what is proven, what is
not, and why.

## Quick start

```bash
uv sync --extra dev
uv run pytest                      # 318 tests, no network required
uv run mlb-edge init
```

## Running the backfill

The backfill is a multi-hour, multi-gigabyte pull and belongs on a box with
persistent disk and unrestricted egress.

```bash
mlb-edge backfill --start 2021-03-01 --end 2025-11-01
mlb-edge build-umpire-ratings --start 2021-04-01 --end 2025-10-01
mlb-edge verify                    # the Milestone 1 acceptance gate
mlb-edge status
```

`backfill` is idempotent and resumable: cache TTLs decide what gets re-fetched,
and rows already present at the same as-of timestamp are not duplicated.

### Ingest order

The DAG is ordered because the dependencies are real — game feeds need the
schedule to know which `game_pk`s exist, weather needs venues to know where the
parks are:

```
teams → venues → schedule → game_feeds → transactions → statcast → retrosheet → weather
```

Run a subset with `--sources schedule,game_feeds`.

### Odds and market venues

Configured for **Tier 0** (The Odds API free plan): moneylines only, no
historical backfill, 500 requests/month enforced in code.

```bash
export ODDS_API_KEY=...            # then set sources.odds.enabled: true
mlb-edge odds poll                 # one snapshot into odds_snapshots
mlb-edge odds close --minutes 5    # closing lines, for CLV only
mlb-edge budget
```

Kalshi and Polymarket are the execution venues. Both stay disabled until
credentials are set and — for Kalshi — until the live fee schedule fetch
succeeds. A failed fee fetch halts the trading path rather than falling back to
a remembered formula.

## The poller (start this first)

The archive poller is the one component whose downtime cannot be recovered.
It runs on a real box, not in a dev container:

```bash
sudo ./deploy/deploy.sh                        # see deploy/README.md
mlb-edge poll-status                           # coverage + what the quota buys
mlb-edge import-polls                          # parse the archive into the warehouse
```

It writes raw payloads to timestamped parquet and does no parsing at all, so a
schema change, a parser bug, or a held DuckDB lock cannot stop the archive from
growing. Failures are archived as rows, so a gap in the files means the daemon
was down rather than the upstream being unhappy.

**Every cycle reports completeness, not just success.** It pulls the day's
schedule and logs `captured 13/15 games (kalshi)` per venue, alerting on any
shortfall. This is the check that catches a silently truncated board — the
failure that returns HTTP 200 on every request and logs a clean cycle. It runs
*after* the archive write, so it can never cost bytes.

A per-venue heartbeat survives restarts and alerts via Telegram when a venue
goes quiet for 30 minutes **during a slate** (off-slate silence is correct and
does not alert). Daily backups go off-box with checksums and a restore path
that has been run end to end, not assumed.

Before trusting a single row, work through
[`reports/poller-first-24h.md`](reports/poller-first-24h.md) — exact commands,
expected numbers, and healthy vs. truncated side by side.

The Odds API free tier is 500 credits/month and bills per region per market per
call, which is **2.6 days** of 15-minute polling. The poller reads the remaining
quota off the API's response header and spreads it over the days to
`poller.season_end_date`, self-throttling to roughly three-hourly rather than
going dark. A paid tier restores 15-minute polling with no config change.
Details and the arithmetic are in [`deploy/README.md`](deploy/README.md).

## The projector

In-house, Statcast-based, point-in-time. Produces the eight-way PA outcome
multinomial the simulator draws from.

```bash
mlb-edge build-pa-outcomes                                  # pitches -> plate appearances
mlb-edge build-projections --start 2021-04-01 --end 2026-09-28
```

`build-pa-outcomes` streams through Parquet in fixed-size chunks, so peak
memory is one chunk regardless of scope — all eleven seasons at once is no
heavier than one. Use `--keep-staging` to inspect the chunks.

Strikeouts, walks and hit-by-pitches are counted. Balls in play are **not**: the
count of them is, and they are distributed across hit types by a league contact
table applied to the player's own exit velocities and launch angles. A .380
BABIP over 200 batted balls is mostly defence and park; his exit velocity is his
own and settles far faster.

Regression constants are fit by empirical Bayes at every snapshot, per bucket,
rather than borrowed from published stabilisation points. `n_effective` in
`pa_rates` is the Dirichlet concentration, so a 40-PA player produces a
genuinely wider game distribution rather than a falsely confident one.

See [`reports/milestone2-projector.md`](reports/milestone2-projector.md) — in
particular the two silent bugs found on synthetic data, either of which would
have made the projector inert while emitting plausible output.

## The gate on `model/`

No simulator code exists yet, and none may be written until, in this order:

1. `mlb-edge verify` runs clean — zero ERRORs.
2. The fitted strikeout constant passes the pre-registered ratio check.

```bash
mlb-edge gate          # runs both, writes reports/gate.json, exits non-zero if blocked
```

The record **expires**. It carries a fingerprint of the warehouse it was
computed from — row count and latest as-of per table, hashed — and the test
refuses a record whose fingerprint no longer matches, naming what moved. Row
counts alone would miss a Statcast restatement that revises values without
adding rows; as-of alone would miss a deletion. One clean run does not unlock
`model/` forever.

The gate compares the **≥300 PA** fit, not the shrinkage fit: the target was
derived from qualified-hitter spread, so the comparison has to use a comparable
population.

The order is enforced: a constant fitted on a warehouse that fails its own
integrity checks is a number derived from corrupted input, so it is not
evaluated at all until integrity passes.

`tests/test_model_gate.py` refuses any module under `src/mlb_edge/model/`
unless `reports/gate.json` exists and says the gate passed — so this is a test
failure, not a note in a README.

### The pre-registered target

`features/preregistration.py` fixes the expected strikeout concentration
**before** any real data is seen, derived rather than eyeballed:

```
k = mu(1-mu)/sd^2 - 1  =  0.22 x 0.78 / 0.055^2 - 1  =  55.7
```

from a hitter K% mean of ~22% and true-talent SD of ~5.5pp.

This is **one estimate, not a triangulation.** The published "K% stabilises at
~60 PA" figure is the same identity evaluated from a different published input
— reliability is `n/(n+k)`, which is 0.5 exactly at `n = k`, so the
stabilisation point *is* k. (The `+1` belongs only to `σ² = μ(1-μ)/(k+1)`.)
Agreement means the inputs are mutually consistent, not that either is right. Read 55.7 as "somewhere in the tens"; the width of the ratio band is
what actually protects against a mis-specified estimator.

Fail condition, fixed in advance: **fitted/expected outside [0.5, 2.0]**.
`build-projections` prints the comparison automatically, alongside the ratio at
each playing-time threshold:

```
ratio vs population
  min_pa>=0     n=260   k=    1,352  ratio=  24.27
  min_pa>=200   n=156   k=      890  ratio=  15.98 *primary
  min_pa>=300   n=109   k=    1,020  ratio=  18.32
```

Published spreads are measured on qualified hitters, so fitting across everyone
catches call-ups, widens observed spread and pulls k *down*. If the ratio moves
with the threshold, a low-side miss is sample composition; if it holds steady,
the estimator is the suspect. Hitters only — pitcher K% talent is spread
differently, so pitcher constants are reported but never gated.

## Park orientations

`cf_bearing_deg` (home plate to dead centre, degrees true north) is unset for
all 30 active parks, so wind components resolve to null and
`require_orientation` raises rather than guessing. A wrong bearing does not
degrade gracefully: a 180-degree error is a plausible number that silently
inverts every wind adjustment at that park.

```bash
mlb-edge parks bearings --missing-only         # what is left
uv run pytest tests/test_wind_sign_convention.py
```

The convention is pinned by tests written before any bearing was entered:
meteorological direction is where the wind blows **from**, so a 90-degree wind
at a 90-degree-bearing park blows *in* from centre. Enter `wrigley_field`
first — the suite checks it against the fact that a south-westerly blows out
there, which is what catches a 180-degree flip.

## Design rules the code enforces

These are not conventions. Breaking them requires editing code that exists to
stop you.

| Rule | Enforcement |
|---|---|
| Every table has an as-of axis | `tests/test_schema_contract.py` walks the registry |
| Features never read the closing line | `pit.as_of()` refuses `closing_lines` at any flag |
| Features never read labels by accident | `OUTCOME` tables need `allow_outcomes=True` |
| Everything keys on `game_pk` | schema contract test; `GameMatcher` refuses ambiguous doubleheaders |
| Raw bytes cached before parsing | `RawCache` is append-only and version-stamped |
| No hardcoded fee schedules | Kalshi fees are fetched or trading halts |
| No hardcoded response shapes | endpoints and field maps live in `config/settings.yaml` |
| Naive datetimes rejected | `timeutil.ensure_utc` raises rather than assuming UTC |
| **Degraded paths announce themselves** | see below |

### Standing rule: if it can be silently wrong, it gets a voice

Three bugs in this repo had the same shape — code took a degraded path and
reported success. A dropped Kalshi cursor returned HTTP 200 while discarding
most of the board. A config key nested under the wrong block paced the odds
budget against a guessed horizon. A saturated regression constant flattened
every hitter to league average while emitting well-formed output.

So: **any fallback, default, or degraded path announces itself at WARN and is
pinned by a test.** Examples currently enforced —
`poller.season_end_date` missing, Telegram unconfigured, Kalshi auth
half-configured, a page-limit bound being hit, a backup that never left the box.

### Standing rule: a number about someone else's system carries its provenance

The rule above catches paths that announce themselves *because something went
wrong*. There is a second class that never goes wrong, and so never announces
anything: **a config value asserting a fact about an external system, written
from memory, that the code then obeys as though the venue imposed it.**

Two of these were found by looking, not by failing. `tier: 0` restricted the
odds request to `h2h` for 25 days on a plan that paid for more.
`monthly_request_budget: 500` described a 20,000-credit plan, making every
resolution figure in two reports forty times too pessimistic. Neither was ever
wrong in a way the code could see — obeying them *is* the code's job.

So: **any config value that asserts a fact about an external system carries how
it was established — `# measured <date>` or `# UNVERIFIED` — and anything
UNVERIFIED has a probe command that exists.**

The provenance comment goes in when the number is written, not in a later
sweep. A sweep only finds what someone thought to look for; `reports/invented-constraints.md`
is the one that was done, and section B is what it still has open.

Probes currently shipping:

| Command | Settles |
|---|---|
| `mlb-edge probe-billing` | credits per call, and the real plan size |
| `mlb-edge probe-ratelimit` | the request rate a venue actually allows |
| `mlb-edge probe-depth` | orderbook levels served, and what the cap has already cost |
| `mlb-edge probe-hydrate` | which StatsAPI hydrate terms are still accepted |


## Rebuilding from cache

The raw cache is append-only, so the warehouse is reconstructible from bytes
alone — and reconstructible *as of a past date*, ignoring later upstream
revisions:

```bash
mlb-edge reload-cache statcast                              # rebuild from disk
mlb-edge reload-cache statcast --as-of 2025-06-01T00:00:00Z # as it looked then
```

This is how you tell whether a backtest result moved because the model changed
or because Savant restated the data underneath it.

## Correcting a field map

Three sources (FanGraphs, Kalshi, Polymarket) have response shapes that could
not be verified offline. Their parsers are driven by config, so a correction is
a YAML edit and a reparse, not a re-fetch:

```bash
mlb-edge odds poll                 # or any source, once
mlb-edge probe fangraphs           # dump the observed keys
$EDITOR config/settings.yaml       # fix sources.fangraphs.field_map
mlb-edge reload-cache projections  # reparse cached bytes
```

## Offline smoke test

```bash
python scripts/smoke_offline.py /tmp/mlb-smoke
mlb-edge status --root /tmp/mlb-smoke
mlb-edge verify --root /tmp/mlb-smoke
```

Seeds a throwaway warehouse from test fixtures. Not real data.

## Layout

```
config/         settings, park overrides, book roles and consensus weights
src/mlb_edge/
  config.py     YAML + ${ENV} loading, validation
  timeutil.py   UTC discipline, park-local slate dates
  http.py       rate limiting, bounded retries, no TLS bypass
  pit.py        point-in-time reads and the leak guards
  integrity.py  the checks `verify` runs
  storage/      rawcache (append-only), schema registry, DuckDB warehouse
  poll.py       the standalone archive poller (no warehouse, no parsing)
  ingest/       one module per source, the game_pk matcher, the poll importer
  market/       price conversions (devig lives here from Milestone 3)
deploy/         systemd units and the deploy script
tests/          318 tests, all offline, fixtures + a synthetic generator
```
