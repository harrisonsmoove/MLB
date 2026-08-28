# mlb-edge

MLB game simulation and betting system. The success criterion is **closing line
value against Pinnacle's no-vig close**, not backtested ROI.

Current state: **Milestone 1 (ingestion + warehouse + point-in-time integrity).**
See [`reports/milestone1.md`](reports/milestone1.md) for what is proven, what is
not, and why.

## Quick start

```bash
uv sync --extra dev
uv run pytest                      # 162 tests, no network required
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

The Odds API free tier is 500 credits/month and bills per region per market per
call, which is **2.6 days** of 15-minute polling. The poller reads the remaining
quota off the API's response header and spreads it over the days to
`poller.season_end_date`, self-throttling to roughly three-hourly rather than
going dark. A paid tier restores 15-minute polling with no config change.
Details and the arithmetic are in [`deploy/README.md`](deploy/README.md).

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
tests/          162 tests, all offline, fixtures under tests/fixtures/
```
