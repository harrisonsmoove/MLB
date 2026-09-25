# Postmortem: Kalshi against a sharp consensus

**Status: closed, 2026-09-25. Not pursued.**

Written for someone who was not here. It assumes no prior knowledge of this
repository and no stake in the answer.

---

## 1. What the strategy was

Kalshi lists binary contracts on MLB game winners: "will Toronto beat
Baltimore", trading between 0 and 1 dollar, settling at 1 if true. Sportsbooks
list the same game as a moneyline. A sportsbook's price carries a margin
(the "vig"), which can be removed arithmetically to recover the price the book
thinks is fair. Pinnacle is the reference because it runs the thinnest margin
and takes the largest bets, so its de-vigged price is the closest thing the
market has to a consensus truth.

The trade: when Kalshi's price disagrees with that consensus by more than the
cost of transacting, buy the cheap side and expect Kalshi to move toward the
consensus.

The success criterion, fixed before any measurement: **closing line value**
against Pinnacle's no-vig close — did the price move our way by more than
costs — not backtested profit, which is far easier to manufacture.

## 2. What it costs to transact

Any gap has to clear a floor of three terms:

```
floor(P) = fee(P) + spread/2 + alignment
```

* **Kalshi's fee** is `0.07 x C x P x (1-P)`, maximal at a coin flip: **1.75
  percentage points** of notional at P=0.50, 0.63pp at P=0.10 or 0.90.
* **Half the bid-ask spread**, about 1pp on a 2-cent book.
* **Alignment**: our two data feeds are not sampled at the same instant, so
  part of any apparent gap is just timing error. A gap smaller than that error
  is not a gap.

At a coin flip with a 2-cent spread the floor is **2.75pp**. That number is the
whole story of this postmortem.

## 3. What was measured

25 days of forward collection: a poller taking Kalshi orderbooks and Pinnacle
moneylines every few minutes, archived as raw payloads with the fetch time on
every row. 464,917 Kalshi orderbook snapshots. 345 games joined on both venues.

| Quantity | Measured |
|---|---|
| Qualifying gaps above the floor | **39** in 25 days (~1.6/day) |
| Mean convergence toward the consensus, at the close | **+1.46pp** |
| Floor it had to clear | **2.70pp** |
| Sign of the convergence | Positive — Kalshi did move our way |
| Adverse selection | None detected — we were not the stale side |

**The sign was right and the magnitude was not.** Kalshi drifts toward the
sharp consensus, by about half of what it costs to be there.

## 4. What killed it

Convergence would have to roughly **double** — 1.46pp to above 2.70pp — and
none of the available levers move it that far.

**The fee cannot be avoided by picking prices.** The fee is largest at a coin
flip, so the obvious response is to trade only heavy favourites where it is
small. Measured across the range baseball actually prices at:

```
     P      fee   floor @ 1c half-spread   relief vs 0.50
  0.50    1.75pp                  2.75pp           0.00pp
  0.60    1.68pp                  2.68pp           0.07pp
  0.65    1.59pp                  2.59pp           0.16pp
  0.75    1.31pp                  2.31pp           0.44pp
  0.80    1.12pp                  2.12pp           0.63pp
```

A -300 favourite is P=0.75 and a -400 is P=0.80; baseball rarely goes past
that, and most games sit between 0.35 and 0.65 where the fee is within 0.2pp of
its maximum. Tail pricing buys back **at most 0.63pp**, on a small minority of
games. It does not close a 1.24pp shortfall.

**Order book depth was never the constraint.** An early config error recorded
only 10 price levels when real books run 22-30, which was a permanent data
loss. It never bound the result: the gaps were not failing for lack of size at
the touch.

**Adverse selection was not the problem either.** The fear was that Kalshi
being "wrong" usually means Kalshi knows something — that we would be the slow
side. The archive says otherwise. That is the one encouraging finding, and it
is why the conclusion is "too small", not "backwards".

**The only untested lever was speed** — catching gaps earlier, before Kalshi
drifts, by polling faster. The staleness analysis showed the binding
constraint was the *odds* feed's cadence, throttled for most of the archive by
a wrong budget figure since corrected. So a faster-polling rerun might have
produced more gaps. It would not obviously have produced *bigger* ones, and
bigger is what was needed. October was not spent finding out.

## 5. What would have changed the answer

Recorded so this is re-checkable rather than a matter of taste:

* **A materially lower fee.** At half the current fee the floor at a coin flip
  falls to about 1.88pp and +1.46pp is close. A fee change at the venue, or a
  venue with a different schedule, reopens this.
* **A tighter spread.** The floor assumes 1pp of half-spread. A book that is
  reliably 1 cent wide takes 0.5pp off.
* **Convergence above ~2.7pp** on a sample large enough to trust. The study
  prints the number and its threshold side by side on every run; a future
  archive can rerun it unchanged.
* **A sport where the same structure holds with less efficient pre-game
  pricing.** Not pursued here: the scope for this project is baseball only.

What would *not* have changed it: more data at the same magnitude, a better
de-vig method (the spread between methods is 0.00pp at a pick'em and 1.17pp at
-250, and the tension was settled by measurement), or faster polling on its
own.

## 6. The defects, and the fact that they pointed one way

The first complete run reported **303,372 qualifying gaps averaging 20
percentage points** and a passing verdict. Every number in it was wrong. Seven
distinct defects were found and fixed; the important pattern is *which
direction they pushed*.

### The four that corrupted the measurement

| # | Defect | Effect |
|---|---|---|
| 1 | **Side inversion.** Kalshi tickers name which team the YES side pays on (`...TORBAL-BAL`). The study keyed prices on `sorted(teams)`, which by construction discards that, and compared them against the de-vigged **home** probability. | Inflated every away-side gap by exactly `1 - 2p`: **20 points on a 60/40 game**. |
| 2 | **In-play contamination.** The sharp feed is pre-match; its last quote freezes at first pitch while Kalshi trades all nine innings. Nothing filtered to pre-game. | Inflated gaps — a live price differenced against a frozen one. |
| 3 | **Matchup collision.** Both legs keyed on the team pair with no date. The same two teams meet three or four times a week, so every meeting merged into one series with one first pitch and one close. | Inflated the row count and attached each gap's "close" to the wrong game. |
| 4 | **Both sides double-counted.** Kalshi lists one market per side; both resolve to the same game. Fixing #3 collapsed them correctly and then used both. | Doubled n with observations that are near-complements, not independent. |

**Three of the four inflated the headline, and the fourth inflated the sample
size.** None of them pushed toward "no edge". That is not coincidence and it is
the single most useful thing in this document: **a pipeline assembled by
someone who wants to find an edge fails toward finding one.** Errors in a
measurement are not symmetric when the person building it has a preference.

Two independent checks would have caught the whole class before a verdict was
ever printed, and neither existed:

* **A plausibility bound.** Two venues pricing the same baseball game do not
  disagree by 20 points. Anything above about 25pp is a comparison error, not a
  finding. Now asserted, with violations excluded and reported.
* **An end-to-end record dump.** One gap printed in full — ticker, resolved
  side, both sides of the book, the raw prices, all four de-vig methods, the
  oriented comparison — makes an inversion obvious immediately. Aggregates hid
  it across 300,000 rows.

### The three that corrupted the reporting

| # | Defect | Effect |
|---|---|---|
| 5 | **Series contamination.** The poller collects totals markets too, whose ticker suffix is a *strike* (`-11` is eleven runs). Read as a side, they produced "codes" 5 through 12 and were reported as 297,859 failed side resolutions. | Implied 87% data loss where the truth was a market-type filter. The moneyline sample was never affected. |
| 6 | **Manufactured ties.** The ticker-to-game joiner was written for a single day's slate and compared start times by *time of day*. Across a multi-day archive a series starting 19:05 every night tied Monday against Tuesday against Wednesday, and all were refused as ambiguous doubleheaders. | **Suppressed** the sample: 258 games refused against 200 joined, before the fix; 2 against 345 after. The only defect that pushed the other way. |
| 7 | **A diagnostic looser than the thing it checked.** A probe counted a market as quoting in-play whenever it was *listed* after first pitch; the study required *prices*. The same number then carried two opposite verdicts in two outputs — "many" in one, "near zero" in the other. | Neither was a measurement. Both were adjectives on a bare count. |

Defect 7 has the broadest lesson. The fix was not a better adjective, it was a
**denominator**: 2,715 in-play quotes against the number of in-play moments the
poller actually observed, with the first fifteen minutes past a *scheduled*
start excluded from both sides, because a 19:05 that throws at 19:12 leaves the
pre-game price standing and that is not in-play coverage.

### A near-miss worth recording

While tidying the accounting, the obvious fix to an unbalanced bucket count was
to mark every unmatched game "not listed by the venue". That would have been
wrong: `not_listed` deliberately **removes a game from the completeness
denominator**, so widening it would have turned every future matcher failure
into "not expected" and shrunk the denominator silently. The bug this repo
spent a week removing, nearly reintroduced as a tidiness change. It got a
separate bucket instead, and two tests.

## 7. A result that looked like good news and was not

The corrected run showed the toward-rate climbing with gap size: 46.2% for
gaps under 4pp, 93.1% at 4-6pp, 100.0% at 6-10pp. A monotonic climb to 100%
reads as an edge that grows with opportunity size.

It is what a **noisy mid price** produces with no edge present at all, and it
takes two mechanisms. Simulated, with no edge anywhere:

* Counting a close *identical* to the gap price as "not toward" — the commonest
  outcome on a thin book, and commonest at small gaps — drags the low buckets
  below a coin flip and produces a climb topping out near 54%.
* A book that is wide when the market opens and tight at the close makes every
  early "gap" mostly noise in a mid nobody could trade on, and the tightening
  reads as convergence. That gives **100% at every bucket above 4pp**.

The discriminator is now reported: the realised book spread at the gap moment
against the spread at the close. If the first is much wider than the second,
the pattern is the artefact.

## 8. What was kept

* **The archive.** 25 days of point-in-time Kalshi orderbooks and sharp
  moneylines, every row stamped with its fetch time, cannot be reconstructed
  after the fact — there is no historical orderbook endpoint. The poller keeps
  running. Postseason data costs nothing to collect and has value for whatever
  comes next.
* **The instrumentation.** Every exclusion is counted and printed before any
  verdict: in-play, unresolvable side, stale, under floor, implausible,
  plus per-day rates and the episode count. A future strategy inherits a
  measurement harness that reports what it threw away.
* **Three standing rules**, each bought with a specific bug. They are in the
  README:
  1. *If it can be silently wrong, it gets a voice.* Any fallback or degraded
     path logs at WARN and is pinned by a test.
  2. *A number about someone else's system carries its provenance.* Every
     config value asserting an external fact is marked `# measured <date>` or
     `# UNVERIFIED`, and anything unverified has a probe command that exists.
  3. *Any price carries what it is the probability of.* A bare number with two
     possible referents gets read against the wrong one, and on a binary market
     that error is `1 - 2p` — largest exactly where the money is.

A fourth is implicit in defect 7 and worth stating: **a diagnostic must be at
least as strict as the thing it checks**, or it will certify what the real code
rejects.

## 9. The honest summary

The strategy was tested against a pre-registered threshold and did not clear
it. Convergence is real, positive, and about half the size it needs to be. The
costs that make it unprofitable are structural — a fee maximal exactly where
baseball prices, on a venue whose spread we do not set — and the one untested
lever, speed, plausibly produces more gaps rather than bigger ones.

Closed in September rather than April, on 39 observations rather than a season
of them. The four measurement bugs all pointed toward a positive result, and it
still came back negative. That is the most reassuring thing about the negative.
