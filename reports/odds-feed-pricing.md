# Pricing the sharp feed

Written before any WebSocket code, because what is affordable on the sharp side
decides what dislocation is measurable at all, and a measurement threshold
pre-registered against an unaffordable feed pre-registers nothing.

Date: 2026-09-24.

---

## 0. What I could and could not verify

**Every odds-vendor domain is blocked at this container's egress gateway** —
the-odds-api.com, theoddsapi.com, odds-api.io, sportsgameodds.com, oddspapi.io,
pinnodds.com. Same policy that has blocked every data host since Milestone 1.

So the prices below are **search-derived and unverified**, and several come from
competitors' comparison pages, which are marketing. Treat every dollar figure as
a number to confirm on the vendor's own pricing page before acting.

What is *not* second-hand, and does not depend on any of it:

* the credit arithmetic in section 3, computed from the billing rule the
  existing integration already relies on;
* the friction floor in section 5, computed from Kalshi's published fee formula;
* the conclusion in section 7, which follows from those two.

The pricing uncertainty turns out not to matter, for reasons section 7 gives.

---

## 1. The structural fact that reframes this

**Pinnacle closed its public API on 23 July 2025.** Access is now "select high
value bettors and commercial partnerships" by application to api@pinnacle.com.

This is the single most important thing in this document and it was not in the
plan. Every consequence follows:

* There is no first-party sharp feed to buy at any advertised price. Everything
  below is a **reseller** — a scraper, an aggregator, or a partner redistributing
  under terms you do not control.
* A reseller can be cut off. Pricing a strategy on one is pricing it on a
  dependency that Pinnacle can end unilaterally, and did once already.
* The redistribution latency is added to Pinnacle's own. A vendor claiming
  "15-40 ms from a Pinnacle price change" is claiming that for their own
  detection, not for the round trip from the book.
* Applying directly is free and worth doing before paying a reseller. The worst
  case is a no.

A second consequence, which affects the strategy and not just the budget: if
Pinnacle's price is harder for everyone to get, the population of people
arbitraging Kalshi against it is smaller than it was in 2024. That cuts both
ways — less competition for the edge, and less reason to believe the edge is
real, since fewer eyes also means fewer people to have already confirmed it.

---

## 2. Tiers and prices (UNVERIFIED — confirm before acting)

| Provider | Tier | Cost/mo | Quota | Push? | Pinnacle |
|---|---|---|---|---|---|
| the-odds-api.com *(current)* | Free | $0 | 500 credits | no | via `eu` region |
| the-odds-api.com | — | ~$30 | 20,000 credits | no | yes |
| the-odds-api.com | — | ~$59 | 100,000 credits | no | yes |
| the-odds-api.com | — | ~$249 | 15,000,000 credits | no | yes |
| pinnapi | Free | $0 | 100 REST req/day | no | Pinnacle-only |
| pinnapi | paid | from ~$99 | not stated | SSE + WS | Pinnacle-only |
| pinnodds | Pro | ~$99 | not stated | REST only | Pinnacle-only |
| pinnodds | Pro + SSE | ~$149 | not stated | SSE push | Pinnacle-only |
| pinnodds | Scale | ~$229 | not stated | WS + SSE | Pinnacle-only |
| odds-api.io | — | not obtained | — | WS at 2x REST price | yes |
| OpticOdds | enterprise | "high hundreds"+ | quote | real push feeds | 200+ books |
| OddsJam | API | quote-only, reported $499–$5,000 | quote | — | yes |

Two cautions on this table:

**There are at least three distinct vendors with near-identical names** —
`the-odds-api.com` (the one this repo integrates), `theoddsapi.com`, and
`odds-api.io`. Search results conflate them freely, and the two tier structures
I found ($30/$59/$249 by credit volume, versus Free/$29 Professional/$99
Business by feature) may belong to different companies. Confirm on the domain
the API key actually authenticates against.

**"From $99" for a Pinnacle-only reseller is not comparable to "$30 for 20,000
credits"** at a multi-book aggregator. The first is one book with push; the
second is fifty books by polling.

---

## 3. Credit arithmetic (exact)

The Odds API bills **one credit per region per market per call**. The current
poller uses `regions=us,eu` and `markets=h2h`, so two credits a call. That is
the rule the existing quota pacing already relies on, so this table is arithmetic
rather than a claim.

Sustainable interval, polling only during the slate (~11 h/day, 330 h/month):

| Request shape | credits/call | free (500) | ~$30 (20k) | ~$59 (100k) | ~$249 (15M) |
|---|---|---|---|---|---|
| `eu`, h2h | 1 | 39.6 m | 59 s | 12 s | — |
| `us,eu`, h2h *(current)* | 2 | 79.2 m | 2.0 m | 24 s | — |
| `eu`, h2h + totals | 2 | 79.2 m | 2.0 m | 24 s | — |
| `us,eu`, h2h + totals | 4 | 2.6 h | 4.0 m | 48 s | — |
| `eu`, h2h + totals + F5 | 4 | 2.6 h | 4.0 m | 48 s | — |
| `us,eu`, h2h + totals + F5 | 8 | 5.3 h | 7.9 m | 1.6 m | 1 s |

Three things fall straight out:

1. **Dropping `us` halves every cost.** If Pinnacle is the reference and the
   recreational books carry zero consensus weight anyway — which `books.yaml`
   already enforces — the `us` region is being paid for and discarded. That is
   the cheapest single improvement available and it needs no new vendor.
2. **~$30/month buys sub-minute Pinnacle h2h polling.** That is the headline: the
   feed the plan called for is cheap, on the vendor already integrated, with no
   code change beyond a config edit.
3. **At ~$249 the quota stops binding and the per-second rate limit takes over.**
   Beyond that tier you are buying rate limit, not credits.

---

## 4. Markets by tier

Reported, not verified: **MLB first-5-innings markets require the Business tier**
on the provider that publishes a Free/Professional/Business structure. Totals
appear at the Professional level.

This matters more than the price does. The plan's three deciding markets are F5
moneyline, F5 total and pitcher strikeout props. On the free tier you have
**h2h only** — which means, unchanged, the entire strategy runs against full-game
moneyline, the most efficiently priced market in the sport.

That is survivable for *this* trade in a way it would not be for the simulator,
because the claim here is not "beat Pinnacle" but "Kalshi disagrees with Pinnacle
and Pinnacle is right". Still: Kalshi's MLB board is mostly game winner, so
full-game h2h is the honest comparable, and the F5 markets are a 2027 question
rather than an October one.

---

## 5. The friction floor — the number that actually decides this

Kalshi charges `0.07 × C × P × (1−P)`, rounded up per order. It is maximal at a
coin flip, which is where most MLB moneylines sit.

| Contract price | Fee/contract | As pp of $1 | % of capital | Gross edge needed* |
|---|---|---|---|---|
| $0.10 | $0.0063 | 0.63 pp | 6.30% | **1.63 pp** |
| $0.20 | $0.0112 | 1.12 pp | 5.60% | **2.12 pp** |
| $0.30 | $0.0147 | 1.47 pp | 4.90% | **2.47 pp** |
| $0.50 | $0.0175 | 1.75 pp | 3.50% | **2.75 pp** |
| $0.70 | $0.0147 | 1.47 pp | 2.10% | **2.47 pp** |
| $0.90 | $0.0063 | 0.63 pp | 0.70% | **1.63 pp** |

\* fee plus 1 pp for crossing half a 2-cent spread. This is the edge at which
you break even — the first dollar of profit comes after it.

**A 2 pp edge at a coin flip is not a small edge. It is a loss.**

Two implications worth carrying forward:

* **The trade is structurally better at the tails.** Fee halves by $0.20 or
  $0.80. Unfortunately that is also exactly where devig methods diverge most
  (1–2 pp between multiplicative and Shin on a heavy favourite), so the place
  the economics improve is the place the measurement gets least reliable. Those
  two effects are the same size. That tension should be pre-registered, not
  discovered.
* **Market-making does not escape the fee.** Kalshi charges it on execution
  regardless of whether you made or took. Making earns the spread instead of
  paying it — worth roughly 2 pp at a 2-cent spread — but the 1.75 pp fee stays.
  At a coin flip that is close to a wash; at the tails it is a real business.
  So the market-making fallback is a *tails* strategy, and it needs measuring
  separately rather than assuming it inherits this analysis.

---

## 6. What the trade returns, bounded by depth

Bankroll is not the constraint and neither is turnover. The constraint is **how
many contracts fill near the dislocated price before it moves**, on a board
thinner than Pinnacle's.

Monthly profit, 450 team-games, after fee and half-spread at $0.50:

| Gross edge | Net/contract | 10% of games × 300 fills | 20% × 300 | 40% × 1,000 |
|---|---|---|---|---|
| 3.0 pp | +0.25 pp | **+$34** | +$67 | +$450 |
| 4.0 pp | +1.25 pp | **+$169** | +$338 | +$2,250 |
| 6.0 pp | +3.25 pp | **+$439** | +$877 | +$5,850 |

Against monthly feed costs of $99 / $149 / $229 / $750.

At a **3 pp** gross edge the strategy does not pay for a $99 feed at any
plausible hit rate and depth. At **4 pp** it clears $99 comfortably and $229
only at high hit rates. At **6 pp** it is a real business — but a persistent
6 pp mispricing against Pinnacle on a liquid US market is a strong claim, and
the correct prior is that it is wrong.

### October specifically

35 postseason games, 20% hit rate, 300 fills, 4 pp gross edge:

> **2,100 contracts → $26 for the whole of October.**

No feed pays for itself this season. Not one. The cheapest is $99.

---

## 7. The honest conclusion

**Do not buy a fast sharp feed yet, and the reason is not that it is expensive.**

Sub-minute Pinnacle h2h polling costs about $30/month on the vendor already
integrated — cheap enough that cost is not the deciding factor. Push delivery
from a Pinnacle reseller is about $99–$229/month, which also is not much.

The deciding factor is that **nothing in this document establishes that the edge
exists**, and October cannot establish it either: 35 games at any believable
edge and depth returns tens of dollars. Buying a feed now would be paying to
accelerate a measurement that the calendar does not permit.

What the numbers do establish, and what changes the plan:

1. **The friction floor is 2.75 pp at a coin flip.** Any dislocation smaller
   than that is not an opportunity. This belongs in the measurement gate as a
   hard floor, and it is a much sharper criterion than "median dislocation
   above X" — it comes from Kalshi's own fee schedule, not from a judgement.
2. **The free tier is enough to find out.** At 40 minutes (`eu` only, h2h) the
   archive still shows whether dislocations above 2.75 pp occur, how often, and
   how long they survive. What 40-minute polling cannot measure is persistence
   *below* 40 minutes — but a dislocation that dies inside 40 minutes is one you
   were never going to trade at 15-minute Kalshi polling anyway.
3. **The measurement is two-stage, and stage one is free.** Stage one: does a
   >2.75 pp gap against Pinnacle occur at all, at what frequency, on the archive
   being collected right now. Only if stage one passes does stage two — buying
   ~$30/month for sub-minute resolution to measure persistence — become worth
   anything.
4. **Halve the credit cost — but not by dropping `us`.** See the correction
   below.

### CORRECTION (2026-09-24): `us` is not discarded data

Section 7 of the first draft said the `us` region "buys nothing the consensus
weights do not already zero out". That is wrong, and checking `books.yaml`
rather than remembering it shows why:

```yaml
pinnacle: 0.55      # eu
circasports: 0.15   # us
bookmaker: 0.10     # us
betonlineag: 0.10   # us
lowvig: 0.05        # us
```

**Forty per cent of the consensus weight sits in `us`-region books.** They are
low-vig and sharp, which is why they carry weight; the earlier claim confused
them with the recreational books that are correctly zeroed. Dropping `us` does
not discard ignored data, it reduces the consensus to Pinnacle alone.

The right move is neither region. The Odds API bills the `bookmakers` parameter
at **one region-equivalent per ten bookmakers**, so naming the five consensus
books explicitly costs **1 credit per market** — the same as `eu` alone, with
the full consensus intact, and it makes the region question moot:

```
bookmakers=pinnacle,circasports,bookmaker,betonlineag,lowvig
```

That halves the current cost without losing anything. It needs a small change
to `OddsPoller.poll` and `credits_per_call`, which currently only understand
regions. Verify the billing empirically before committing: one call, and read
the `x-requests-remaining` delta from the response header.

### On the market-making fallback

Your instinct to redirect there if the feed does not pay is sound, but it is a
different business and this analysis does not transfer. Market-making does not
race a sharp price, so latency stops mattering and the whole feed question goes
away — but the Kalshi fee does not, and at a coin flip it consumes most of a
2-cent spread. It is a **tails** strategy, on $0.15–$0.30 contracts where the
fee is a third of what it is at the middle. It needs its own measurement:
realised spread, fill rate, and adverse selection against informed flow, none
of which the current archive captures.

It is also the one strategy on the table that does **not** depend on a Pinnacle
reseller staying alive, which after July 2025 counts for something.

---

## 8. Recommended reordering

1. **Drop `us` from the odds poller.** Config edit, halves credits, today.
2. **Apply to api@pinnacle.com.** Free, slow, and the only path to a
   first-party feed. Worst case is a no.
3. **Stage-one measurement on the free tier**, against the archive already
   collecting: frequency of >2.75 pp gaps. Pre-register the threshold now.
4. **Only if stage one passes**, buy ~$30/month and measure persistence.
5. **Kalshi WebSocket** after that, not before — it measures the fast side of a
   gap whose existence is still unestablished.
6. **Price the market-making alternative in parallel.** It needs spread and
   depth data the archive is already accumulating, and it costs nothing to
   analyse.

Confirm every price in section 2 before spending anything. I could not reach a
single vendor's pricing page from here.
