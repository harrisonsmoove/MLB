# Pitcher strikeout props: what transfers, what does not, what is missing

Written 2026-09-25, at the close of the market-vs-market strategy. Nothing here
is a recommendation to build yet; the first question is a cost measurement and
it has not been taken.

## The open question, and it is not answerable from documentation

**Does this plan serve player props, and what does one strikeout line cost?**

Both are UNVERIFIED. Tier names, inclusions and prices are exactly the class of
external fact this repo no longer asserts from memory — the last two times it
did, `tier: 0` restricted the odds request for 25 days on a plan that paid for
more, and `monthly_request_budget: 500` made every resolution figure in two
reports forty times too pessimistic.

```bash
mlb-edge probe-props            # dry run, spends nothing, lists what it would try
mlb-edge probe-props --confirm  # measures it
```

The probe does three things rather than one:

1. **Asks the API to enumerate its own legal market keys**, by sending a
   deliberately invalid one. An API that validates the parameter usually lists
   the valid values in the error body, and that list is authoritative where a
   guess is not. `config/settings.yaml` carries four guessed spellings marked
   UNVERIFIED purely so the probe has something to try; they are not a claim.
2. **Measures the credit cost** from the `x-requests-remaining` delta, per
   event per market per region.
3. **Projects a season** at several cadences against the credits actually
   remaining.

That third output is the decision. **Props bill per EVENT, not per slate.** One
moneyline call covered the whole board; one prop call covers one game. Cost now
scales with slate size *and* cadence *and* market count, which is a different
economic shape from everything built so far.

**If the probe says props are not served**, the error body distinguishes the two
possible reasons — a plan restriction versus a wrong market key — and only that
text can. I am not naming a cheapest tier that includes props: I would be
reciting a price list from memory, it changes, and this project has already paid
twice for that. Read the probe's error line, then the vendor's current pricing
page. If the answer is "not on this plan", the probe result is the thing to take
to that page.

## What transfers

Roughly 60% of the repository, and all of the parts that were expensive to get
right.

| Component | Transfers | Why |
|---|---|---|
| `poll.py`, `PollArchive` | **As is** | Raw payloads, hive-partitioned by venue and date, fetch time on every row. Market-agnostic. |
| Point-in-time discipline (`pit.py`, `storage/schema.py`) | **As is** | `as_of_ts` on every table, closing prices quarantined, label reads greppable via `allow_outcomes=True`. This is the part that makes any backtest trustworthy and it took the longest. |
| `market/devig.py` | **As is** | Four methods, refuses an underround book. A two-way over/under prop de-vigs exactly like a moneyline. |
| `market/prices.py` | **As is** | American/decimal/implied conversions. |
| `ingest/odds.py`, `http.py` | **Mostly** | Same vendor, same auth, same rate limiting. The request shape changes from bulk to per-event. |
| `completeness.py` | **Concept, not code** | The machinery that catches "captured 0/0 games" on a full slate is the single most valuable operational lesson here. But it counts *games* against a schedule; props need *pitchers* against expected starters. Rewrite against the same design. |
| `pollhealth.py`, `alerting.py` | **As is** | Heartbeat, staleness, Telegram. |
| `backup.py` | **As is** | Off-box backup with a verified restore. |
| `integrity.py` + the PIT test suite | **As is** | The tests that prove no future data leaks into a past row. |
| `features/shrinkage.py` | **Directly, and this is the point** | `fit_regression_constant`, `HierarchicalPrior`, `recency_weights`, `dirichlet_posterior`. Empirical-Bayes shrinkage of a rate toward a population mean is *exactly* the K-rate problem. |
| `features/pa_outcomes.py` | **Directly** | `BUCKETS = ("K", "BB", "HBP", "1B", "2B", "3B", "HR", "OUT")` — strikeouts are already the first bucket of the extracted plate-appearance data. |
| `features/pitcher_proxies.py`, `ratings.py`, `park.py` | **Mostly** | Pitcher modelling with platoon splits, built for the simulator, applies to a K projection with less work than it needed for a full game sim. |
| `rules.py` | **As is** | Season-conditional DH and extra-innings rules. |
| The three standing rules and the probe pattern | **As is** | See the postmortem. |

## What does not transfer

* **`kalshi_tickers.py`, `ingest/kalshi.py`, `ingest/polymarket.py`** — prediction-market
  specific. ~800 lines retired. The *lesson* transfers: join on identifiers,
  never on display names, and refuse ambiguity rather than guessing.
* **`eval/adverse.py`** — built around a two-venue gap between a prediction
  market and a sharp book. The friction-floor arithmetic (`fee`, `floor_for`,
  `breakeven_toward_rate`) generalises; the Kalshi fee formula does not apply to
  a sportsbook, where the vig *is* the fee and is already inside the price.
* **The Kalshi fee model.** On a book, cost is the hold in the line. Different
  arithmetic, and the friction floor has to be re-derived from scratch rather
  than reused.

## What you listed that does not actually exist

Correcting your read, because planning around it would waste time:

* **CLV tracking — not built.** There is a quarantined `closing_lines` table
  and a schema comment claiming "only `market/clv.py` reads it". `market/clv.py`
  **was never written**. The table is the right shape; the module is empty space.
* **Staking — not built.** No Kelly function, no sizing module, nothing. Kelly
  arithmetic for binary contracts was worked out in conversation
  (`f* = (p_true - P)/(1 - P)`) and never committed. `strategy/` and `model/`
  are empty packages — 0 lines each.

So the honest version of your list is: odds ingestion, devig, the archive, the
completeness machinery, backups and the PIT guarantees transfer. **CLV and
staking are new work**, and CLV matters more for props than it did here,
because it is the only success criterion that resists overfitting.

## What is missing, in order of risk

**1. Batters faced is the real modelling problem, not K-rate.** A strikeout
total is `K-rate x batters faced`, and you are right that K-rate is the most
stable pitcher skill. Batters faced is not a pitcher skill at all — it is a
manager's hook decision, driven by pitch count, score, bullpen availability and
how the third time through the order is going. The variance in a strikeout prop
is dominated by the term that is *least* predictable, and a projection that
nails K-rate and guesses innings will not beat a book. Any feasibility study
starts here, not with K-rate.

**2. Player identity resolution across sources.** Team matching in this repo
needed an alias table, a ticker parser and a doubleheader refusal, and still
produced three bugs. Players are much worse: hundreds of names per slate,
suffixes, accents, nicknames, and no shared key between a book's prop
description and a StatsAPI player id. Budget real time, and refuse rather than
fuzzy-match — the one place this project already learned that lesson the hard
way.

**3. Per-event polling and its cost model.** A new poller shape, a new
completeness check (expected starters, not games), and a budget that scales with
the slate. The probe above sizes it.

**4. Alternate lines and ladders.** Props come as a strike ladder (5.5, 6.5,
7.5) plus alternates. A projection produces a *distribution*, so it prices the
whole ladder at once — which is an advantage over the moneyline case, and a
schema question to settle before ingestion.

**5. Settlement data.** Actual strikeouts per start, to grade against. StatsAPI
already provides it and the ingester exists; the wiring does not.

**6. CLV and staking**, per above.

## One caution on the "not orthogonal to Pinnacle" argument

The reasoning is sound: beating a lightly-attended prop line is a lower bar
than beating the sharpest price in the market, and thousands of props get less
attention per line than one moneyline. Two things it does not remove:

* **Limits are small on props**, so the same edge produces much less profit per
  line and the strategy lives on volume across many lines.
* **Books restrict winners.** Beating a book's prop line is a relationship with
  a counterparty that can decline your action, which the Kalshi structure did
  not involve. That is a business risk, not a modelling one, and it belongs in
  the plan rather than discovered later.

Neither argues against the pivot. Both belong written down before the first line
of code, on the same principle as pre-registering a threshold.

## Suggested first step

Run the probe. If props are served and the cost projection fits the plan,
the next piece of work is **not** a K-rate model — it is a feasibility study on
batters faced, because that is the term that decides whether any K projection
can beat a line. If it cannot be projected well enough, that is worth knowing in
a week rather than after building the rest.
