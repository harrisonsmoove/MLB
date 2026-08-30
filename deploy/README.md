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

**If the branch has not reached GitHub yet**, deploy from a git bundle instead
— `git clone` and `git fetch` both accept a bundle path, so nothing else
changes:

```bash
scp mlb-edge.bundle you@box:/root/mlb-edge.bundle
ssh you@box
git clone --branch claude/mlb-simulation-betting-txwq10 /root/mlb-edge.bundle /tmp/mlb-edge
sudo REPO_URL=/root/mlb-edge.bundle /tmp/mlb-edge/deploy/deploy.sh
```

Keep the bundle on disk: subsequent deploys fetch from the same path. Switch
`REPO_URL` back to the GitHub URL once the branch is pushed.

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

## Alerting — set it up before you need it

Alerts go to the journal always, and to Telegram if configured. Journal-only
means nobody sees them at 3am.

```bash
# 1. @BotFather -> /newbot -> copy the token into TELEGRAM_BOT_TOKEN
# 2. Send your bot any message (Telegram hides the chat id until you do)
# 3. Find the chat id:
mlb-edge test-alert --discover-chat --root /opt/mlb-edge
# 4. Put it in TELEGRAM_CHAT_ID, then prove the whole path:
mlb-edge test-alert --root /opt/mlb-edge
```

`test-alert` validates the token separately from delivery, so a failure tells
you which thing is wrong rather than just "failed":

| Symptom | Meaning |
|---|---|
| `token rejected (401)` | `TELEGRAM_BOT_TOKEN` is wrong |
| `chat not found` | `TELEGRAM_CHAT_ID` is wrong, or you never messaged the bot |
| `bot was blocked` | unblock it in Telegram |
| `could not reach Telegram` | network or egress problem on the box |

It exits non-zero if nothing reached a phone, so it is safe to put in a
post-deploy check.

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


## Box-specific config

`deploy.sh` runs `git reset --hard`, so **hand edits to `config/settings.yaml`
do not survive a deploy**. That is deliberate — it is how shipped config fixes
reach the box — but it means local settings need somewhere else to live.

That place is **`config/local.yaml`**, which is git-ignored, layered over
`settings.yaml` at load time, and never rewritten by deploy after it is first
created:

```yaml
sources:
  odds:
    enabled: true
  kalshi:
    enabled: true
```

The merge is deep, so overriding one key leaves its siblings alone, and the
overridden paths are printed on load — a file that silently changes which
sources are enabled is not something a later debugging session would think to
look for.

The first deploy writes this file, setting each `enabled` flag from whether the
matching credential is actually present in `/etc/mlb-edge/mlb-edge.env`. A
source enabled without its key fails config validation at startup, which is the
right behaviour and an unhelpful thing to arrive at by default.

If `config/` has uncommitted edits when you deploy, they are copied to
`data/config-backups/<timestamp>/` (with a diff) before the reset, and the log
says so.
