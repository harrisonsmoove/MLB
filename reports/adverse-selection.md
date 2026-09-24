# Adverse selection: whose edge is the gap?

Pre-registered 2026-09-24, before any gap has been measured. Uses only the 25
days already archived. Costs nothing and answers a question that makes stage
one meaningless if it comes back wrong.

---

## 1. The question

When Kalshi disagrees with the sharp consensus, two explanations produce an
identical gap distribution:

* **Kalshi is stale.** The sharp price moved, Kalshi has not caught up, and the
  gap is ours to take.
* **Kalshi is informed.** Someone there knows something the sharp books have
  not priced — a scratch, a bullpen note, a weather read — and the gap is
  theirs.

No amount of gap frequency separates these. A board with a 40% hit rate of
large gaps looks equally good under both, and is a business under one and a
slow bleed under the other. On a thin board the second is common, because thin
boards are where informed flow moves the price furthest.

---

## 2. The test

For every historical gap above `floor(P)`, measure what the Kalshi price did
next. **Toward** the sharp price means Kalshi was stale and the gap was real.
**Through** it means we were the stale ones.

This is a closing-line-value measurement wearing different clothes, and that is
the point: CLV against Pinnacle's close is the project's stated success
criterion, and this asks the same question of Kalshi's own close on data
already held.

### Horizons, all reported, one gating

| Horizon | What it answers |
|---|---|
| +1 Kalshi snapshot | immediate reversion — does it start moving at all |
| +4 snapshots (~1 h) | whether it persists long enough to trade |
| **last quote before first pitch** | **the gate.** Kalshi's own closing price |

The closing horizon gates because it is the one that corresponds to holding the
position, which is the simplest strategy and the one with no execution risk in
the measurement.

---

## 3. Why 50% is the wrong threshold

The proposal on the table was: *if gaps resolve against me at or above 50%, the
strategy is dead.* The first half is right — below 50% it is definitively dead.
The second half is where it fails: **above 50% proves nothing, because 50% is
break-even only when friction is zero.**

Buy at `K` when the sharp price says `S`, gap `g = S − K`, hold to settlement.
A fraction `f` of the time the sharp price was right and we gain about `g`.
The rest of the time Kalshi was right, and what we lose depends on why:

```
EV = g·(2f − 1) − friction        if "through" means true value is past K
EV = g·f − friction               if "through" means the gap was simply noise
```

Break-even `f`, at the 2.75 pp friction floor for a coin-flip contract:

| Gap `g` | break-even `f` (adverse) | break-even `f` (noise) |
|---|---|---|
| 2 pp | **impossible** | **impossible** |
| 3 pp | 95.8% | 91.7% |
| 4 pp | 84.4% | 68.8% |
| 5 pp | 77.5% | 55.0% |
| 7 pp | 69.6% | 39.3% |
| 10 pp | 63.7% | 27.5% |
| 15 pp | 59.2% | 18.3% |

**A 60% resolve-toward rate on 3 pp gaps is a losing strategy**, comfortably. A
threshold of 50% would have passed it.

The two columns bracket reality — genuine adverse selection sits nearer the
left, pure noise nearer the right — and the gap between them is wide enough
that **picking either would be choosing an answer.** So the gate does not use
a win rate at all.

---

## 4. The criterion, pre-registered

> **Primary.** For gaps above `floor(P)`, the **mean realised convergence** —
> the signed movement of the Kalshi price toward the sharp price, measured to
> the last quote before first pitch — must exceed `floor(P)`.
>
> Stated per gap-size bucket and pooled. The pooled figure gates.

Magnitudes, not a win rate. The archive holds how far each gap moved, so the
model-dependence in section 3 is avoidable: measure the thing the models were
trying to approximate. A strategy that wins 55% of the time but wins big and
loses small is fine, and a win-rate test would kill it; one that wins 80% of
the time in 1 pp increments and loses 8 pp when wrong is not, and a win-rate
test would pass it.

**Reported alongside, not gating:**

* resolve-toward **rate** per gap bucket, plotted against the break-even curve
  above, so a rate is never read without the threshold it has to clear;
* the **distribution** of realised convergence, not just its mean — a mean
  carried by three outliers is a different finding from a mean carried by the
  body;
* the same figures split by **time to first pitch**, because lineup and weather
  news arrives on a schedule and informed flow with it.

### The hard stop

> If mean realised convergence is **negative** at any gap size above the floor,
> the prediction-market-versus-sharp-book trade is dead and stage one is not
> run. A negative figure means the archive says we are the slow side.

This is the one result that ends it outright rather than resizing it. It costs
nothing to find out and it invalidates everything downstream, which is why it
runs before stage one rather than after.

---

## 5. What a pass licenses

The same restraint as the stage-one gate: **passing is not permission to
trade.** It licenses running stage one at all. Convergence exceeding friction
on historical gaps says the signal is not backwards; it does not say gaps occur
often enough, or that they survive execution, or that they persist below the
15-minute cadence we can currently see.

---

## 6. Known limits of this study

Stated now so they are not discovered as excuses later.

* **The archive's sharp leg was polled at ~19 minutes, not ~1.** Everything
  measured here inherits the alignment error in `stage-one-gate.md` §3a, which
  is larger than it will be going forward. That biases against finding
  convergence, so a pass is trustworthy and a marginal fail is not.
* **Orderbook depth was capped at 10 levels for these 25 days.** Irrelevant
  here — this study uses prices, not depth — but it means the same window
  cannot also answer the size question.
* **25 days is roughly 350 team-games**, and gaps above the floor will be a
  fraction of those. If the qualifying count is under 50 the study is
  underpowered, and the honest output is "not enough gaps yet", not a verdict.
  That number gets reported first, before any convergence figure.
