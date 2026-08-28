# Deploying the poller

The poller is the only component whose downtime cannot be made up later. A
model bug can be fixed and re-run against the archive; an hour not polled is an
hour of closing-line data that does not exist and cannot be bought. Treat its
uptime as the priority.

## Deploy

```bash
git clone https://github.com/harrisonsmoove/MLB /tmp/mlb-edge
sudo /tmp/mlb-edge/deploy/deploy.sh
```

First run installs the units but does **not** start them — it creates
`/etc/mlb-edge/mlb-edge.env` from the example and stops. Fill in credentials,
then re-run the same script to start everything.

```bash
sudo $EDITOR /etc/mlb-edge/mlb-edge.env   # ODDS_API_KEY, KALSHI_API_KEY_ID, ...
sudo /tmp/mlb-edge/deploy/deploy.sh
```

Re-run it for every subsequent deploy. It is idempotent, never overwrites the
secrets file, and restarts the poller last so a failed build leaves the running
poller alone rather than taking the archive down to install something broken.

## What gets installed

| Unit | Cadence | Job |
|---|---|---|
| `mlb-edge-poller.service` | continuous | Polls odds + Kalshi, writes parquet. Restarts forever. |
| `mlb-edge-refresh.timer` | daily 13:00 UTC | Pulls schedule, probables, teams. **Not optional** — see below. |
| `mlb-edge-import.timer` | hourly | Parses the archive into the warehouse. |

`mlb-edge-refresh` is load-bearing. The archive stores a book's team names and a
commence time; resolving those to a `game_pk` needs the schedule and teams
dimension present in the warehouse. Without it the archive keeps growing and
nothing in it can be joined to a game.

## The budget, plainly

The Odds API bills **1 credit per region per market per call**. With
`regions=us,eu` and `markets=h2h` that is 2 credits a poll.

| Plan | Credits/mo | 15-min polling lasts | Sustainable interval to season end |
|---|---|---|---|
| Free | 500 | **2.6 days** | ~3 hours |
| Entry paid (~$30) | 20,000 | 104 days | 15 min (the configured floor) |

The poller reads the remaining quota from the API's own response header on every
call and spreads what is left evenly over the days to
`poller.season_end_date`. On a free key it self-throttles to roughly three-hourly
rather than going dark in September. Raise the plan and it returns to 15-minute
polling on its own, with no config change and no restart needed — the header is
the only input.

Three hours between polls is too coarse to capture a close. If the CLV dataset
matters — and it is the long pole in the whole system — the paid tier is the
cheapest thing on the critical path.

Kalshi is not credit-metered and polls at the full cadence regardless.

## Checking it works

```bash
journalctl -u mlb-edge-poller -f              # live
/opt/mlb-edge/.venv/bin/mlb-edge poll-status --root /opt/mlb-edge
/opt/mlb-edge/.venv/bin/mlb-edge verify --root /opt/mlb-edge
```

`poll-status` prints archive coverage per venue and what the remaining quota
buys. If it says *throttled*, that is the free-tier budget working as designed,
not a fault.

A healthy first hour looks like:

```
[poll] starting: kalshi, odds
[poll] odds tick=1 records=1 errors=0 quota_remaining=498 -> odds-20260828T140000Z.parquet
[poll] odds next in 179.4 min
[poll] kalshi tick=1 records=34 errors=0 -> kalshi-20260828T140000Z.parquet
[poll] kalshi next in 15.0 min
```

## Reading failures

Failures are archived as rows, not dropped. A gap in the file listing means the
daemon was down; a file full of `error` values means the daemon was up and the
upstream was not. The two have different fixes, and the archive distinguishes
them.

```sql
-- duckdb
SELECT venue, endpoint, http_status, error, count(*)
FROM read_parquet('/opt/mlb-edge/data/poll/venue=*/dt=*/*.parquet', hive_partitioning := true)
WHERE error IS NOT NULL
GROUP BY 1,2,3,4 ORDER BY 5 DESC;
```

## Disk

Roughly 5 MB/day compressed (zstd) for both venues at 15-minute cadence — about
150 MB a month. Not a concern on any reasonable droplet, but the archive is the
irreplaceable asset here: **back up `/opt/mlb-edge/data/poll` and nothing else
needs backing up.** The warehouse is rebuildable from it at any time.

## Fixing a parser after the fact

The archive holds the original bytes, so a parser or field-map fix applies
retroactively to everything ever polled:

```bash
mlb-edge probe kalshi --root /opt/mlb-edge     # dump observed keys
sudo $EDITOR /opt/mlb-edge/config/settings.yaml
mlb-edge import-polls --reimport --root /opt/mlb-edge
```

This is why the poller does no parsing. Getting the bytes is urgent and
unrepeatable; understanding them is neither.

## Stopping

```bash
sudo systemctl stop mlb-edge-poller            # graceful: finishes the tick in flight
sudo systemctl disable --now mlb-edge-poller mlb-edge-refresh.timer mlb-edge-import.timer
```
