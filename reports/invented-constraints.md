# Audit: repo-chosen numbers treated as external facts

Requested after `tier: 0` and `rate_limit_per_minute: 60` both turned out to be
this shape. **Reported, not fixed** — the list is the deliverable.

The test applied to every number in `config/settings.yaml`:

> If this value is wrong, does the system behave as though an external
> constraint exists that does not — or fail to obey one that does?

A number that is honestly a preference (how often to back up, how much of the
bankroll to risk) fails that test and is not listed. A number that the code
obeys *as if the venue imposed it* passes, and belongs here whether or not it
has cost anything yet.

Date: 2026-09-24. Read against the config at `a79c422`.

---

## A. Confirmed instances

| Setting | Value | What it cost |
|---|---|---|
| `sources.odds.tier` | 0 | 25 days of h2h-only polling on a plan paying for more. **Fixed.** |
| `sources.odds.monthly_request_budget` | 500 | Real plan is 20,000. Every resolution figure in two reports was 40x pessimistic. **Fixed.** |

Both were invented, both were obeyed, neither ever warned.

---

## B. Same shape, not yet measured — ranked by what they can cost

### B1. `sources.kalshi.orderbook_depth: 10` — **the serious one**

Ten levels of book depth, archived. Nothing verifies this is what Kalshi
returns or the most it will return.

This is the worst item on the list and it is not close, for a reason none of
the others share: **the archive is irreplaceable, and stage two's binding
measurement is depth.** Every revenue estimate in `odds-feed-pricing.md` turns
on how many contracts fill near the dislocated price. If Kalshi serves 50
levels and this asks for 10, then every orderbook snapshot ever taken has been
silently truncated at level 10, and the data needed to answer the question that
decides the project has been discarded at collection time for a month.

It cannot be recovered. There is no historical orderbook endpoint.

**Check first, before the rate limit and before anything else.** One request at
a higher depth against one liquid ticker answers it.

### B2. `sources.statcast.row_cap: 30000`

Commented `# Savant truncates silently above this` — stated as an external
fact. It is a remembered one; no probe established it.

The failure is silent by construction. If the real cap is lower, every chunk
that exceeds it is quietly short and the PA extraction is built on partial
data — the exact failure the streaming work was meant to eliminate, one layer
further up. `chunk_days: 3` is derived from this number, so it inherits the
error.

Mitigating: the existing extractor checks coverage and the 99.5% threshold
would likely catch a large shortfall. Not a small one.

### B3. `sources.kalshi.rate_limit_per_minute: 60`

The one already queued for probing. Bounds the slower leg of the entire
measurement: at ~200 tickers a full sweep takes 200 s, so a 15-minute cadence
leaves a factor of four unused even at the configured rate.

### B4. `sources.statcast.rate_limit_per_minute: 20`

Repo-chosen. Baseball Savant publishes no limit. This is the number the
"eight-hour backfill" estimate rests on — the estimate that justified moving
the k gate ahead of the backfill in the first place. If the real limit is 60,
the backfill is under three hours and that sequencing argument was built on an
invented constraint.

### B5. `sources.odds.rate_limit_per_minute: 10`

Harmless at the current cadence and would bind immediately if the poller ever
needed a burst — a catch-up sweep after an outage, for instance, is exactly
when it would matter and exactly when nobody would be looking.

### B6. `sources.mlb_statsapi.rate_limit_per_minute: 120`

Repo-chosen, free endpoint, no known limit. Lowest stakes here, listed for
completeness because it is the same shape.

### B7. `markets_page_size: 200` / `events_page_size: 200` (kalshi, polymarket)

Is 200 the API maximum or a round number someone typed? If the maximum is
1,000 the poller is making five times the page requests it needs — which feeds
straight back into B3, since pages and orderbooks share the rate budget.

### B8. `sources.retrosheet.publication_year_offset: 1`

"Retrosheet publishes a season's events the following spring." A real external
fact and the right instinct — it is what keeps transition matrices for season
N fit only on seasons before N. But it is a remembered publication schedule,
and if Retrosheet is slower in some year the PIT guarantee it enforces is
wrong in the unsafe direction: data treated as available before it was.

---

## C. Checked and cleared

Not invented constraints. Listed so the audit is falsifiable rather than a
selection of the things I happened to notice:

* **Honest operational choices** — `odds_interval_seconds`, `kalshi_interval_seconds`,
  `keep_local`, `max_age_hours`, `staleness_alert_minutes`, `alert_repeat_hours`,
  all `timeout_seconds` / `max_attempts` / `backoff_*`, `schedule_cache_seconds`,
  the four `completeness_*` window settings. Each is a preference about how this
  system behaves, not a claim about the world.
* **Pre-registered thresholds** — `min_trials_for_constant_fit`, `gate_min_trials`,
  `min_paper_bets_before_live`, `min_bets_for_roi_claim`, `bootstrap_iterations`.
  Deliberately fixed in advance; changing them is the failure mode, not leaving
  them.
* **Modelling parameters** — `recency_halflife_days`, `hierarchy_pooling_k`,
  `platoon_prior_k`, `battedball_pooling_k`, `n_sims`, `random_seed`, and the
  whole `strategy` block. Assumptions to be fitted or justified, and wrong in a
  different way than this audit is about.
* **`season_end_date`** — was an invented constraint until yesterday. Now a
  stated fact about the 2026 calendar, and the pacing bug it caused is fixed.

---

## D. Recommended order

Ranked by irreversibility, not by effort:

1. **`orderbook_depth`** — one request. Every hour it stays wrong discards
   archive that cannot be rebuilt, and it is the measurement stage two exists
   for.
2. **`kalshi.rate_limit_per_minute`** — `mlb-edge probe-ratelimit`, already
   built. Unblocks the cadence work.
3. **`markets_page_size`** — falls out of the same probe session.
4. **`statcast.row_cap` and `rate_limit_per_minute`** — before the backfill,
   not during. Both change how long it takes and whether it is complete.
5. **`odds.rate_limit_per_minute`** — raise to match what the billing probe
   already showed the plan sustains.
6. **`publication_year_offset`** — verify against Retrosheet's actual release
   history before the transition matrices are fit.

---

## E. The pattern worth naming

Every instance has the same signature: **a number that describes somebody
else's system, written from memory, with no mechanism that would ever
contradict it.** They do not fail loudly because they are not wrong in a way
the code can see — the code's whole job is to obey them.

The existing standing rule covers fallbacks and degraded paths, which announce
themselves because something went wrong. This class never goes wrong. It just
quietly costs what it costs.

Proposed addition, in the same spirit:

> **A config value that asserts a fact about an external system carries how it
> was established — `# measured 2026-09-24` or `# UNVERIFIED` — and anything
> unverified is probeable by a command that exists.**

`probe-billing` and `probe-ratelimit` are that mechanism for two of them. The
rest of section B needs the same treatment, and the comment convention is what
makes the gap visible without another audit.
