"""Per-venue heartbeat, persisted across restarts.

The question this answers is not "did the last request work" but "when did this
venue last give us anything, and is that recent enough to be believable given
what is on the schedule right now".

Staleness is only alarming during a slate. A venue quiet at 6am with first pitch
eight hours away is behaving correctly; the same silence at 7pm is data being
lost permanently. Alerting on the first case trains you to ignore the second,
which is the failure mode that actually costs a season.

State lives in a small JSON file next to the archive so a restart -- expected,
since the unit restarts forever -- does not reset the clock and hide an outage
that has been running for hours.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mlb_edge.alerting import Alert, Severity
from mlb_edge.completeness import CoverageReport, ExpectedGame, SlateStatus
from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow


@dataclass
class VenueHealth:
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    consecutive_failures: int = 0
    last_records: int = 0
    total_records: int = 0
    last_coverage: str | None = None

    def success_ts(self) -> datetime | None:
        return parse_iso_utc(self.last_success_at) if self.last_success_at else None

    def staleness(self, now: datetime) -> timedelta | None:
        """How long since this venue last produced anything. None if never."""
        success = self.success_ts()
        return None if success is None else ensure_utc(now) - success


@dataclass
class HealthState:
    path: Path
    venues: dict[str, VenueHealth] = field(default_factory=dict)
    started_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> HealthState:
        path = Path(path)
        state = cls(path=path)
        if not path.is_file():
            return state
        try:
            raw = json.loads(path.read_text("utf-8"))
            state.started_at = raw.get("started_at")
            state.venues = {
                name: VenueHealth(**payload)
                for name, payload in (raw.get("venues") or {}).items()
            }
        except Exception:  # noqa: BLE001 - corrupt state must not stop polling
            print(f"[health] could not read {path}; starting fresh", flush=True)
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "started_at": self.started_at,
            "venues": {name: asdict(health) for name, health in self.venues.items()},
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
        temporary.replace(self.path)

    def for_venue(self, venue: str) -> VenueHealth:
        return self.venues.setdefault(venue, VenueHealth())

    def record_attempt(
        self,
        venue: str,
        *,
        records: int,
        errors: int,
        coverage: CoverageReport | None = None,
        now: datetime | None = None,
    ) -> None:
        """Log one cycle's outcome for a venue.

        A cycle counts as a success only if it produced at least one record that
        was not an error. A tick that returns nothing but failure rows is an
        attempt, not a heartbeat -- treating it as one is how a venue that has
        been 500-ing for hours looks healthy.
        """
        reference = ensure_utc(now or utcnow())
        health = self.for_venue(venue)
        health.last_attempt_at = reference.isoformat()
        health.last_records = records
        health.total_records += records
        if coverage is not None:
            health.last_coverage = coverage.line()

        if records > errors:
            health.last_success_at = reference.isoformat()
            health.consecutive_failures = 0
        else:
            health.consecutive_failures += 1


def staleness_alerts(
    state: HealthState,
    *,
    slate: list[ExpectedGame],
    now: datetime | None = None,
    threshold: timedelta = timedelta(minutes=30),
    slate_lead: timedelta = timedelta(hours=6),
    slate_trail: timedelta = timedelta(hours=5),
) -> list[Alert]:
    """Alerts for venues that have gone quiet while games are in play or imminent."""
    reference = ensure_utc(now or utcnow())
    if not _slate_active(slate, reference, slate_lead, slate_trail):
        return []

    alerts: list[Alert] = []
    for venue, health in sorted(state.venues.items()):
        staleness = health.staleness(reference)
        if staleness is None:
            alerts.append(
                Alert(
                    key=f"never-succeeded:{venue}",
                    severity=Severity.CRITICAL,
                    subject=f"{venue}: no successful poll ever recorded",
                    body=(
                        "Games are on the board and this venue has never returned "
                        "usable data. Check credentials and the journal."
                    ),
                )
            )
            continue
        if staleness >= threshold:
            minutes = staleness.total_seconds() / 60
            alerts.append(
                Alert(
                    key=f"stale:{venue}",
                    severity=Severity.CRITICAL,
                    subject=f"{venue}: no successful poll for {minutes:.0f} min",
                    body=(
                        f"Last success {health.last_success_at}. "
                        f"{health.consecutive_failures} consecutive failed cycles. "
                        "Tier 0 has no historical endpoint, so anything missed now "
                        "is missed permanently."
                    ),
                )
            )
    return alerts


def coverage_alerts(
    reports: list[CoverageReport], *, min_expected: int = 1
) -> list[Alert]:
    """Alerts for venues covering fewer games than the schedule says exist.

    This is the check that catches a silently truncated board -- the failure
    that returns HTTP 200 on every request and logs a clean cycle.

    It used to skip any report with ``expected == 0``, which meant the check
    fell silent in exactly the state where it had gone blind. A missing
    denominator now alerts louder than a shortfall does, because a shortfall is
    a known quantity and a missing denominator is not.
    """
    alerts: list[Alert] = []
    for report in reports:
        if report.status is SlateStatus.UNAVAILABLE:
            alerts.append(
                Alert(
                    key=f"coverage:{report.venue}",
                    severity=Severity.CRITICAL,
                    subject=f"{report.venue}: schedule unavailable, coverage UNKNOWN",
                    body=(
                        f"The MLB schedule fetch failed: {report.slate_error}\n"
                        "The poller is still archiving, but nothing is checking whether "
                        "the board is complete. A truncated board would look clean "
                        "until this is fixed."
                    ),
                )
            )
            continue

        if report.contradicted:
            alerts.append(
                Alert(
                    key=f"coverage:{report.venue}",
                    severity=Severity.CRITICAL,
                    subject=(
                        f"{report.venue}: {report.in_progress} games in progress, "
                        "0 expected"
                    ),
                    body=(
                        f"StatsAPI reports {report.in_progress} game(s) under way and "
                        f"the completeness check expects none, from a slate of "
                        f"{report.slate_size}.\n"
                        "No window or timezone setting makes that benign -- the slate "
                        "window is wrong and coverage is not being checked."
                    ),
                )
            )
            continue

        if report.expected < min_expected or report.complete:
            continue
        alerts.append(
            Alert(
                key=f"coverage:{report.venue}",
                severity=Severity.CRITICAL if report.covered == 0 else Severity.WARN,
                subject=(
                    f"{report.venue}: captured {report.covered}/{report.expected} games"
                ),
                body=_shortfall_body(report),
            )
        )
    return alerts


def _shortfall_body(report: CoverageReport) -> str:
    """Enough to tell a matcher gap from a truncated board without an ssh session.

    Those need opposite responses: one is a counting bug with the data safely
    archived, the other is permanent loss on a source with no historical
    endpoint. "MISSING 13" says nothing about which.
    """
    lines = [
        "Missing: "
        + ", ".join(report.missing[:8])
        + ("" if len(report.missing) <= 8 else f" (+{len(report.missing) - 8} more)")
    ]

    uncounted = report.present_but_uncounted
    absent = [m for m in report.missing if m not in set(uncounted)]
    if uncounted:
        lines.append(
            f"\n{len(uncounted)} of these ARE in the payload but were not counted "
            "-- a matcher gap, not data loss:"
        )
        lines.extend(f"  {label}" for label in uncounted[:5])
    if absent:
        lines.append(
            f"\n{len(absent)} do not appear at all -- that is real loss, and Tier 0 "
            "has no historical endpoint to re-poll:"
        )
        lines.extend(f"  {label}" for label in absent[:5])

    if report.sample_labels:
        lines.append("\nWhat the payload actually contains:")
        lines.extend(f"  {value}" for value in report.sample_labels[:8])
    else:
        lines.append(
            "\nNo recognisable title or ticker strings in the payload at all. "
            "The shape is not what the matcher assumes."
        )
    lines.append("\n`mlb-edge explain-coverage --venue <venue>` for the full picture.")
    return "\n".join(lines)


def frozen_alerts(fingerprint: Any, identical_ticks: int, *, threshold: int = 2) -> list[Alert]:
    """Alert when consecutive ticks return byte-identical payloads.

    Row count cannot separate a healthy board from a frozen one: five ticks of
    ``records=123`` looked like stability and was a saturated cap. Content can.
    An orderbook whose every payload hashes the same twice running is not a
    quiet market -- prices and depth move -- it is a cache, a replay, or an
    upstream that has stopped updating.

    ``threshold`` is 2 rather than 1 because a genuinely dead overnight board
    can repeat once; twice is not chance.
    """
    if identical_ticks < threshold:
        return []
    return [
        Alert(
            key=f"frozen:{fingerprint.venue}",
            severity=Severity.CRITICAL,
            subject=(
                f"{fingerprint.venue}: {identical_ticks} consecutive ticks byte-identical"
            ),
            body=(
                f"{fingerprint.records} records, {fingerprint.keys} distinct keys, "
                f"{fingerprint.bodies} distinct payloads, digest "
                f"{fingerprint.digest[:12]}.\n"
                "Identical content across polls means the archive is accumulating "
                "duplicate rows with fresh timestamps, which is worse than a gap: it "
                "looks like data."
            ),
        )
    ]


def _slate_active(
    slate: list[ExpectedGame],
    now: datetime,
    lead: timedelta,
    trail: timedelta,
) -> bool:
    return any(game.start_ts - lead <= now <= game.start_ts + trail for game in slate)


def health_summary(state: HealthState, *, now: datetime | None = None) -> list[str]:
    reference = ensure_utc(now or utcnow())
    lines: list[str] = []
    for venue, health in sorted(state.venues.items()):
        staleness = health.staleness(reference)
        age = "never" if staleness is None else f"{staleness.total_seconds() / 60:.0f} min ago"
        lines.append(
            f"{venue:<12} last success {age:<14} "
            f"records {health.total_records:,}  failures {health.consecutive_failures}"
            + (f"  [{health.last_coverage}]" if health.last_coverage else "")
        )
    return lines
