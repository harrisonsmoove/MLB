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
from enum import StrEnum

#: Status codes StatsAPI uses to reject a hydrate string. 406 is what it
#: actually returns; 400 is included because a rejected term is a malformed
#: request by any reading and other deployments have reported it that way.
HYDRATE_REJECTION_STATUSES = frozenset({400, 406})


def hydrate_string(terms: Sequence[str]) -> str:
    return ",".join(terms)


def base_term(term: str) -> str:
    """``probablePitcher(note)`` -> ``probablePitcher``.

    The distinction that matters. A rejected sub-hydration does not mean the
    term is gone: ``probablePitcher(note)`` returns 406 while
    ``probablePitcher`` returns 200. Reporting the first as "probablePitcher
    rejected" costs the probable starters -- which the simulator keys on, and
    whose absence reads as "no starter announced" rather than as a bug.
    """
    head, _, _ = term.partition("(")
    return head.strip()


def strip_sub_hydrations(terms: Sequence[str]) -> list[str]:
    """Every term reduced to its base, de-duplicated, order preserved."""
    seen: list[str] = []
    for term in terms:
        base = base_term(term)
        if base and base not in seen:
            seen.append(base)
    return seen


class Verdict(StrEnum):
    KEEP = "keep"
    #: The sub-hydration is rejected but the base term is accepted. The term
    #: stays, stripped -- this is the case that must never be reported as DROP.
    STRIP = "strip"
    DROP = "drop"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class TermResult:
    term: str
    status: int | None
    ok: bool
    detail: str = ""
    #: Result of probing the bare base term, when the configured form failed
    #: and differs from its base. ``None`` when that probe was not needed.
    base_ok: bool | None = None
    base_status: int | None = None

    @property
    def base(self) -> str:
        return base_term(self.term)

    @property
    def verdict(self) -> Verdict:
        if self.ok:
            return Verdict.KEEP
        if self.status not in HYDRATE_REJECTION_STATUSES:
            return Verdict.INCONCLUSIVE
        if self.base_ok:
            return Verdict.STRIP
        if self.base_ok is None and self.base != self.term:
            return Verdict.INCONCLUSIVE
        return Verdict.DROP

    @property
    def replacement(self) -> str | None:
        """What this term should become in the config. ``None`` means remove it."""
        if self.verdict is Verdict.KEEP:
            return self.term
        if self.verdict is Verdict.STRIP:
            return self.base
        if self.verdict is Verdict.INCONCLUSIVE:
            return self.term
        return None

    def describe(self) -> str:
        if self.verdict is Verdict.KEEP:
            return "accepted"
        if self.verdict is Verdict.STRIP:
            return f"sub-hydration REJECTED, base '{self.base}' accepted -> strip"
        if self.verdict is Verdict.DROP:
            return "REJECTED, base too -> remove"
        return f"inconclusive ({self.detail or self.status})"


@dataclass
class BisectResult:
    baseline_ok: bool
    baseline_detail: str
    terms: list[TermResult]
    combined_ok: bool | None = None

    @property
    def accepted(self) -> list[str]:
        return [t.term for t in self.terms if t.verdict is Verdict.KEEP]

    @property
    def stripped(self) -> list[TermResult]:
        return [t for t in self.terms if t.verdict is Verdict.STRIP]

    @property
    def rejected(self) -> list[str]:
        return [t.term for t in self.terms if t.verdict is Verdict.DROP]

    @property
    def inconclusive(self) -> list[str]:
        return [t.term for t in self.terms if t.verdict is Verdict.INCONCLUSIVE]

    def corrected_terms(self) -> list[str]:
        """The hydrate list this probe says the config should hold."""
        out: list[str] = []
        for entry in self.terms:
            replacement = entry.replacement
            if replacement and replacement not in out:
                out.append(replacement)
        return out

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
        lines: list[str] = []
        for entry in self.stripped:
            lines.append(
                f"  {entry.term}  ->  {entry.base}   (keep the term, drop the "
                "sub-hydration)"
            )
        for term in self.rejected:
            lines.append(f"  {term}  ->  (remove)")
        if not lines:
            return (
                "Every term was accepted as configured. If the combined call still "
                "fails, the problem is the combination or its length, not one term."
            )
        return "Change sources.mlb_statsapi.schedule_hydrate:\n" + "\n".join(lines)


def bisect_terms(
    terms: Sequence[str],
    probe: Callable[[list[str]], tuple[bool, int | None, str]],
) -> BisectResult:
    """Find which hydrate terms the API no longer accepts, and in what form.

    Each term is probed **twice when it matters**: as configured, and -- if that
    is rejected and the term carries a sub-hydration -- stripped to its base.
    Without the second probe a rejected ``probablePitcher(note)`` is reported as
    "probablePitcher rejected", and the probable starters get dropped from the
    config over a sub-field.

    Linear rather than a true binary bisect on purpose: with six terms it is a
    handful of cheap unauthenticated requests, and it distinguishes *two* bad
    terms from one, which a binary search reports as a single culprit.

    ``probe`` takes a term list and returns ``(ok, status, detail)``. The
    baseline is ``probe([])``.
    """
    baseline_ok, _, baseline_detail = probe([])

    results: list[TermResult] = []
    for term in terms:
        ok, status, detail = probe([term])
        base_ok: bool | None = None
        base_status: int | None = None
        if not ok and status in HYDRATE_REJECTION_STATUSES:
            base = base_term(term)
            if base and base != term:
                base_ok, base_status, _ = probe([base])
        results.append(
            TermResult(
                term=term,
                status=status,
                ok=ok,
                detail=detail,
                base_ok=base_ok,
                base_status=base_status,
            )
        )

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
