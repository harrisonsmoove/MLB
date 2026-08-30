"""StatsAPI hydrate terms: bisecting them, and surviving without them.

StatsAPI rejects the **entire request** with HTTP 406 when it does not recognise
one hydrate term. It does not ignore the term and return what it can. So a
single term going stale -- which happens, the vocabulary is undocumented and
changes -- takes down every request that carries the string, and the failure
looks nothing like "your hydrate is wrong": it is a 406 on a URL that worked
last season.

That cost real time here. One stale term in a six-term string returned 406 on
every schedule fetch, which meant the completeness check had no denominator and
reported ``captured 0/0`` for as long as it was deployed, and would also have
failed the first step of the backfill.

Two defences, and they are different in kind:

**Structural.** The completeness check does not send hydrate at all. It needs
game_pk, teams, start time and game type, none of which require hydration. A
monitoring path must not share a failure mode with the thing it monitors.

**Degrading.** The ingester does want the hydrated fields, so it retries without
them rather than returning nothing -- loudly, and naming what is now missing.
Partial data with a named gap beats no data with a stack trace, but only if the
gap is named; silently returning unhydrated rows would put nulls in
``probable_pitchers`` that look like "no starter announced".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Status codes StatsAPI uses to reject a hydrate string. 406 is what it
#: actually returns; 400 is included because a rejected term is a malformed
#: request by any reading and other deployments have reported it that way.
HYDRATE_REJECTION_STATUSES = frozenset({400, 406})


def hydrate_string(terms: Sequence[str]) -> str:
    return ",".join(terms)


@dataclass(frozen=True)
class TermResult:
    term: str
    status: int | None
    ok: bool
    detail: str = ""

    @property
    def verdict(self) -> str:
        if self.ok:
            return "accepted"
        if self.status in HYDRATE_REJECTION_STATUSES:
            return "REJECTED"
        return f"inconclusive ({self.detail or self.status})"


@dataclass
class BisectResult:
    baseline_ok: bool
    baseline_detail: str
    terms: list[TermResult]
    combined_ok: bool | None = None

    @property
    def accepted(self) -> list[str]:
        return [t.term for t in self.terms if t.ok]

    @property
    def rejected(self) -> list[str]:
        return [t.term for t in self.terms if not t.ok and t.status in HYDRATE_REJECTION_STATUSES]

    @property
    def inconclusive(self) -> list[str]:
        return [
            t.term
            for t in self.terms
            if not t.ok and t.status not in HYDRATE_REJECTION_STATUSES
        ]

    def suggestion(self) -> str:
        if not self.baseline_ok:
            return (
                "The bare call failed too, so this is not a hydrate problem. "
                f"Fix the connection first: {self.baseline_detail}"
            )
        if self.inconclusive:
            return (
                "Some terms could not be classified (see above). Re-run before "
                "changing the config -- a timeout is not a rejection."
            )
        if not self.rejected:
            return (
                "Every term was accepted individually. If the combined call still "
                "fails, the problem is the combination or its length, not one term."
            )
        return "Remove from sources.mlb_statsapi.schedule_hydrate:\n  - " + "\n  - ".join(
            self.rejected
        )


def bisect_terms(
    terms: Sequence[str],
    probe: Callable[[list[str]], tuple[bool, int | None, str]],
) -> BisectResult:
    """Find which hydrate terms the API no longer accepts.

    One request per term plus a bare baseline and a combined check. Linear
    rather than a true binary bisect on purpose: with six terms it is eight
    cheap unauthenticated requests, and it distinguishes *two* bad terms from
    one, which a binary search reports as a single culprit.

    ``probe`` takes a term list and returns ``(ok, status, detail)``. The
    baseline is ``probe([])``.
    """
    baseline_ok, _, baseline_detail = probe([])
    results = [
        TermResult(term=term, status=status, ok=ok, detail=detail)
        for term in terms
        for ok, status, detail in [probe([term])]
    ]
    combined_ok = None
    if baseline_ok and all(r.ok for r in results):
        combined_ok = probe(list(terms))[0]
    return BisectResult(
        baseline_ok=baseline_ok,
        baseline_detail=baseline_detail,
        terms=results,
        combined_ok=combined_ok,
    )


def is_hydrate_rejection(status: int | None) -> bool:
    return status in HYDRATE_REJECTION_STATUSES
