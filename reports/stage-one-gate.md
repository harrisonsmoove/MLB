# Stage one: does a tradeable gap exist at all?

Pre-registered 2026-09-24, before any gap has been computed. Nothing in here
was chosen after seeing a number, and the numbers that are not yet fixed say
exactly how they will be fixed and from what.

The question stage one answers is not "how big is the edge". It is the cheaper
one that has to come first: **does a gap large enough to be worth trading occur
at all, often enough to matter?** If it does not, no feed purchase, no
WebSocket, no execution engine.

---

## 1. The floor, as a function of price

A gap is only an opportunity if it clears the cost of taking it. Kalshi
publishes its fee schedule, so most of this is arithmetic rather than judgement:

```
fee(P)    = 0.07 · P · (1 − P)          dollars per $1 contract
floor(P)  = fee(P) + s(P)/2
```

where `P` is the Kalshi contract price and `s(P)` the bid-ask spread at that
price. The fee term is exact and maximal at a coin flip, which is where most
MLB moneylines sit:

| P | fee(P) | floor(P) at s = 2c | floor(P) at s = 1c |
|---|---|---|---|
| 0.10 | 0.63 pp | 1.63 pp | 1.13 pp |
| 0.20 | 1.12 pp | 2.12 pp | 1.62 pp |
| 0.30 | 1.47 pp | 2.47 pp | 1.97 pp |
| 0.40 | 1.68 pp | 2.68 pp | 2.18 pp |
| 0.50 | 1.75 pp | **2.75 pp** | 2.25 pp |
| 0.60 | 1.68 pp | 2.68 pp | 2.18 pp |
| 0.70 | 1.47 pp | 2.47 pp | 1.97 pp |
| 0.80 | 1.12 pp | 2.12 pp | 1.62 pp |
| 0.90 | 0.63 pp | 1.63 pp | 1.13 pp |

**The spread term is measured, not assumed.** `s(P)` comes from the archived
Kalshi orderbooks, binned by price, computed **before the first gap is
calculated**. That involves no model output — it is a property of the board we
already hold — so it can be fixed in advance without leaking, exactly as the
run-distribution targets were.

The indicative figures above use a flat 2-cent spread, which is what the
pricing analysis assumed. Recording them here means any movement is visible.

**The measurement runs in both directions.** If the realised spread is wider
than 2 cents the floor rises and the gate gets harder; if tighter, the floor
falls and it gets easier. It is a correction for what the book actually costs
to cross, not a licence held in reserve. Both the indicative and the measured
floor go in the repo.

---

## 2. The devig tension, settled

The economics improve at the tails — fee halves by $0.20 or $0.80. The
measurement gets *worse* at the tails: multiplicative, additive, Shin and power
devig agree to about 0.2 pp near a pick'em and diverge by 1–2 pp on a heavy
favourite. Those two effects are the same magnitude, so a "gap" at the tails
could be entirely an artifact of which devig was chosen.

Arbitrating between methods would be picking the one whose answer we like.
Instead the disagreement becomes a **robustness requirement**:

> A gap counts as qualifying only if it exceeds `floor(P)` under **all four**
> devig methods — multiplicative, additive, Shin and power.

This dissolves the tension rather than adjudicating it. The gate's verdict no
longer depends on the method choice at all, which is the right property when
neither of us can justify one method over the others from first principles. It
is deliberately conservative: it under-counts real gaps at the tails, and
under-counting is the error that costs money we would not have made, rather
than the one that costs money we have.

Shin remains the **reported** primary for eventual pricing, per the earlier
decision. It does not decide the gate. The per-method spread on every candidate
gap is recorded as a diagnostic, because "the methods disagreed by 1.8 pp here"
is the single most useful thing to know about a tail gap.

---

## 3. Persistence

A gap present in one snapshot and absent from the next was never tradeable. At
free-tier resolution (~40 minutes, `eu`-only h2h) the only persistence statement
available is:

> A qualifying gap must appear in **at least two consecutive snapshots**.

That means it survived ~40 minutes. It is a conservative proxy: it misses gaps
that live 5 minutes, which is correct, because a 5-minute gap is not tradeable
against 15-minute Kalshi polling either. It is the reason stage one does not
need a paid feed — the resolution we lack is resolution we could not act on.

Measuring persistence *below* 40 minutes is precisely what stage two buys, and
only if stage one passes.

---

## 4. Pass criteria

Unit of observation: **one game**. Over its pre-game quoting window, did a
qualifying gap ever appear?

> **Stage one passes if qualifying gaps appear on ≥ 10% of games, with the
> lower bound of a 95% confidence interval above 5%.**

Both numbers are derived from the economics in `reports/odds-feed-pricing.md`,
not chosen for feel:

* At **10%** of games with 300 fills and a 4 pp gross edge, the depth-bounded
  return is about $169/month. That clears the ~$30 `eu`-only tier by five times
  and a ~$99 push feed by nearly two.
* Below **5%**, nothing clears $99 at any believable depth. A strategy that
  cannot pay for its own data is not a strategy.

The confidence bound matters because the sample is small. Roughly 350 usable
team-games gives a standard error near 1.6 pp on a 10% rate, so 10% versus 0% is
easy to establish and 10% versus 5% is marginal — which is exactly why the
criterion is a bound rather than a point estimate.

**Report the frequency by price bucket as well as pooled.** If qualifying gaps
occur only at the tails, that is a different and more interesting finding than a
uniform rate, and it points at market-making rather than at taking.

---

## 5. What PASSING means

**Passing stage one is not permission to trade. It is permission to buy
resolution and run stage two.**

Recorded here explicitly, before any number exists, because the failure mode is
obvious in advance and invisible in the moment: a passing frequency in October
becomes live size in October, on a board measured at 40-minute resolution, in
the 35-game window the pricing analysis already showed returns about $26.

What a pass licenses, in order:

1. Buy sub-minute resolution on the sharp side.
2. Measure **persistence below 40 minutes** — the thing stage one structurally
   cannot see, and the thing that decides whether a gap is reachable.
3. Measure **realised depth** at the dislocated price, walked down the book,
   rather than assumed.
4. Paper-log against live quotes until the paper log and the measurement agree.

Only then, size. The gate answers "does a gap exist"; it does not answer "can I
get filled at it", and those are different questions with different failure
modes.

## 6. Why the consensus stays at two credits

An `eu`-only request would halve the spend and reduce the consensus to Pinnacle
plus the prediction-market slot — which is **the venue being priced against**.
`min_books_for_consensus: 2` would still be satisfied, so nothing would warn,
and the gate would be comparing Kalshi to a reference that partly *is* Kalshi.

A passing gate built on that would be an artifact of the circularity, not a
finding, and it would look exactly like a real result.

So the consensus keeps its four `us`-region sharp books at two credits a call
until the `bookmakers` parameter is verified to bill at one. The halving is
worth having; it is not worth having at the cost of the reference.

## 7. What failure means

Stage one failing does not mean try a different devig, a different floor, or a
different window. The floor comes from a published fee schedule; the frequency
thresholds come from what it costs to run the strategy. Both were fixed before
any gap was computed.

It means the prediction-market-versus-sharp-book trade does not exist at this
bankroll on this board, and the next move is the **market-making** alternative —
which does not race a sharp price, does not depend on a Pinnacle reseller
surviving, and is a tails business for the same fee-curve reason that makes
taking hardest in the middle.

That alternative needs its own measurement — realised spread, fill rate,
adverse selection against informed flow — and none of it is captured by the
current archive. Designing that is the work stage one failing would trigger,
not a retry of stage one.
