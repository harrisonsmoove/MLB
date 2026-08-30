# First 24 hours: what to check by hand

Check this manually before trusting the alerting. The alerting is new code and
has never run against a real slate; the first day's job is to find out whether
it would have told you the truth.

Everything below assumes `/opt/mlb-edge` and the `mlbedge` service user. Set
`E=/opt/mlb-edge/.venv/bin/mlb-edge`.

---

## 0. The first ten minutes

```bash
journalctl -u mlb-edge-poller -f
```

A healthy first cycle:

```
[poll] starting: kalshi, odds
[poll] archive: /opt/mlb-edge/data/poll
[poll] odds tick=1 records=1 errors=0 quota_remaining=498 -> odds-20260829T140000Z.parquet
[poll] odds captured 11/11 games (odds, exact)
[poll] odds next in 179.4 min
[poll] kalshi tick=1 records=34 errors=0 -> kalshi-20260829T140000Z.parquet
[poll] kalshi captured 11/11 games (kalshi, team-mention)
[poll] kalshi next in 15.0 min
```

Three things to read, in order of importance:

| Line | What it tells you | Bad looks like |
|---|---|---|
| `captured X/Y games` | **whether the board is complete** | `captured 3/11` |
| `records=N errors=0` | requests worked | `errors=34` |
| `quota_remaining` | budget is being read from the API | absent after several cycles |

**`captured X/Y` is the one that matters.** Requests returning 200 tells you
almost nothing — the Kalshi cursor bug did exactly that while discarding most
of the board. If `captured` is short and `errors=0`, that is the signature.

Expect `odds` to poll roughly three-hourly on the free tier and `kalshi` every
15 minutes. `odds` at 15-minute intervals means the quota header was not read
and the budget will be gone in 2.6 days.

---

## 1. After one hour

```bash
$E poll-status --root /opt/mlb-edge
```

Expect roughly:

```
venue     files   size      first                 last
kalshi    4       1.2 MB    20260829T140000Z      20260829T144500Z
odds      1       0.0 MB    20260829T140000Z      20260829T140000Z

days left in season window: 30
odds quota remaining: 496 credits (2 per call = 248 calls)
throttled: sustaining the season needs 174 min between polls, not the
configured 15 min. At 15 min this quota lasts 2.6 days.
```

`throttled` is **correct behaviour**, not a fault. It is the free-tier budget
being spread over the remaining season.

---

## 2. After 24 hours — the numbers

### Cadence

```bash
duckdb -c "
SELECT venue,
       count(*) AS files,
       min(fetched_at) AS first_seen,
       max(fetched_at) AS last_seen,
       round(count(*) / (date_diff('minute', min(fetched_at), max(fetched_at)) / 60.0), 1)
         AS files_per_hour
FROM read_parquet('/opt/mlb-edge/data/poll/venue=*/dt=*/*.parquet', hive_partitioning := true)
GROUP BY 1 ORDER BY 1;"
```

| Venue | Expected files/24h | Expected files/hour |
|---|---|---|
| kalshi | ~96 | ~4 |
| odds (free tier) | ~8 | ~0.33 |
| odds (paid tier) | ~96 | ~4 |

Substantially fewer means the daemon restarted or stalled. Check
`systemctl status mlb-edge-poller` and `journalctl -u mlb-edge-poller | grep -c starting`
— more than one `starting` line is a restart.

### Records and errors

```bash
duckdb -c "
SELECT venue, endpoint,
       count(*) AS records,
       sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
       count(DISTINCT key) AS distinct_keys
FROM read_parquet('/opt/mlb-edge/data/poll/venue=*/dt=*/*.parquet', hive_partitioning := true)
GROUP BY 1,2 ORDER BY 1,2;"
```

Healthy, on a ~15-game slate:

```
venue   endpoint    records  errors  distinct_keys
kalshi  markets     288      0       3          <- 3 series x 96 ticks
kalshi  orderbook   3800+    0       40-120     <- one per open market per tick
odds    live_odds   8        0       0
```

**`distinct_keys` on `kalshi/orderbook` is the truncation canary.** It should be
in the tens — roughly the number of open MLB markets. If it is stuck at a round
number like exactly 200, or implausibly small like 3, pagination is capped.

### Distinct games actually covered

```bash
duckdb -c "
SELECT dt, count(*) AS market_records
FROM read_parquet('/opt/mlb-edge/data/poll/venue=kalshi/dt=*/*.parquet', hive_partitioning := true)
WHERE endpoint = 'markets' AND payload IS NOT NULL
GROUP BY 1 ORDER BY 1;"
```

Cross-check the game count against the schedule for the same day:

```bash
curl -s "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=$(date -u +%F)" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); \
      print(sum(len(x['games']) for x in d['dates']), 'games scheduled')"
```

Then the same number as the poller saw it:

```bash
journalctl -u mlb-edge-poller --since "24 hours ago" | grep "captured" | tail -20
```

Every line should read `captured N/N`. **Any line where the two numbers differ
is the thing to investigate**, even if it recovered on the next cycle.

---

## 3. Healthy vs. truncated, side by side

The failure mode has no errors in it. That is what makes it dangerous.

| Signal | Healthy | Truncated board |
|---|---|---|
| `errors` in the archive | 0 | **0** — identical |
| HTTP statuses | all 200 | **all 200** — identical |
| `captured X/Y` | `15/15` | `4/15` |
| `distinct_keys` on orderbook | 40–120, varies daily | a flat round number, or very small |
| orderbook records per tick | tracks the slate size | constant regardless of slate |
| Telegram | silent | `kalshi: captured 4/15 games` |

The only columns that differ are the completeness ones. If you take one thing
from this document: **`errors=0` is not evidence of anything.**

---

## 4. Alerting — prove it fires before you rely on it

Do not wait for a real outage to find out whether alerts work.

```bash
# 1. Does Telegram deliver at all?
source /etc/mlb-edge/mlb-edge.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=mlb-edge alert test"

# 2. Does a stale venue alert? Stop the poller mid-slate and wait ~35 min.
sudo systemctl stop mlb-edge-poller
#    ... expect: "kalshi: no successful poll for 3X min"
sudo systemctl start mlb-edge-poller
```

If step 1 works and step 2 does not, the heartbeat is not firing and the
`>30 min` guarantee is not real. Check `data/poll/poll_health.json` — it should
show a `last_success_at` receding into the past.

Note alerts only fire **during a slate** (a game within 6 hours before or 5
hours after first pitch). Testing at 4am will correctly produce nothing.

---

## 5. Backup — restore it once, now

A backup that has never been restored is a hypothesis.

```bash
sudo -u mlbedge $E backup create --root /opt/mlb-edge

# Restore into a scratch directory, NOT over the live one.
LATEST=$(ls -d /opt/mlb-edge/data/backups/* | tail -1)
sudo -u mlbedge $E backup restore "$LATEST" --into /tmp/restore-test

# Row counts must match the live warehouse.
duckdb /tmp/restore-test/data/warehouse/mlb_edge.duckdb \
  -c "SELECT 'games' t, count(*) FROM games UNION ALL SELECT 'teams', count(*) FROM teams;"
duckdb /opt/mlb-edge/data/warehouse/mlb_edge.duckdb \
  -c "SELECT 'games' t, count(*) FROM games UNION ALL SELECT 'teams', count(*) FROM teams;"

rm -rf /tmp/restore-test
```

Then confirm it actually left the machine:

```bash
aws s3 ls s3://your-bucket/mlb-edge/     # or: rclone ls spaces:your-bucket/mlb-edge/
```

If `backup.push_command` is empty in `settings.yaml`, the backup is on the same
droplet as the thing it is protecting and is not a backup.

---

## 6. What "working" looks like at the end of day one

- [ ] `poll-status` shows both venues with files, and a `last` timestamp within one poll interval
- [ ] Every `captured` line in the journal reads `N/N`
- [ ] `errors` column is 0 across the archive — **and** the captured counts are full
- [ ] `distinct_keys` on `kalshi/orderbook` is in the tens and varies with the slate
- [ ] `odds` quota is decreasing by ~2 per poll and the interval is throttled
- [ ] A test Telegram message arrived
- [ ] A stop-the-poller test produced a staleness alert
- [ ] A backup restored into a scratch dir with matching row counts
- [ ] The backup exists in object storage

Anything unticked is worth resolving before day two, because at Tier 0 there is
no way to go back and collect a day again.

---

## 7. When to stop and ask

Stop the poller and investigate rather than accumulating more data if:

- `captured` is persistently short on either venue — the archive is being
  written but it is not the board, and every hour of it is wrong in a way that
  cannot be repaired later.
- `distinct_keys` is pinned to a round number — pagination is capped.
- Quota is falling faster than ~2 credits per logged odds poll — something is
  polling that is not in the journal.

A poller that is off is recoverable in minutes. A poller writing systematically
incomplete data for a week is a week that has to be thrown away, and at Tier 0
it cannot be re-collected at any price.
