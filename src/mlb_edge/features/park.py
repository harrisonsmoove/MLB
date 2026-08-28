"""Park registry and orientation.

The orientation question is narrow but load-bearing: to know whether a 15 mph
wind helps or hurts, you need the compass bearing from home plate to centre
field. Without it a wind speed is just a number with no sign.

``cf_bearing_deg`` is deliberately null until measured. A guessed bearing does
not degrade gracefully -- it produces a confidently wrong feature, and a
180-degree error turns every "wind blowing out" day at that park into a "wind
blowing in" day. Missing is visible; wrong is not. So
:func:`require_orientation` raises rather than defaulting, and the wind
components stay null until a real value is entered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mlb_edge.config import Settings


class OrientationUnknown(RuntimeError):
    """Raised when a wind feature is requested for a park with no bearing.

    Deliberately fatal. The alternative -- silently substituting zero, or north,
    or the league mean -- produces a number that looks like a measurement and is
    not one.
    """


@dataclass(frozen=True)
class Park:
    slug: str
    names: tuple[str, ...]
    altitude_ft: int | None
    roof: str
    cf_bearing_deg: float | None
    orientation_source: str
    neutral_site: bool
    active_from: int | None
    active_through: int | None
    notes: str = ""

    def is_active(self, season: int) -> bool:
        """Whether this park hosts regular-season home games in ``season``.

        Neutral sites are excluded: they host a handful of games a year and are
        not part of the 30-park coverage target, though they still need a
        bearing before their games can be priced on wind.
        """
        if self.neutral_site:
            return False
        not_yet_opened = self.active_from is not None and season < self.active_from
        already_retired = self.active_through is not None and season > self.active_through
        return not (not_yet_opened or already_retired)

    @property
    def has_orientation(self) -> bool:
        return self.cf_bearing_deg is not None


def load_parks(settings: Settings) -> list[Park]:
    defaults: dict[str, Any] = settings.parks.get("defaults", {}) or {}
    parks: list[Park] = []
    for entry in settings.parks.get("parks", []) or []:
        merged = {**defaults, **entry}
        names = (entry.get("match") or {}).get("venue_name")
        if isinstance(names, str):
            names = [names]
        parks.append(
            Park(
                slug=str(entry["slug"]),
                names=tuple(names or []),
                altitude_ft=merged.get("altitude_ft"),
                roof=str(merged.get("roof", "none")),
                cf_bearing_deg=_bearing(merged.get("cf_bearing_deg"), entry["slug"]),
                orientation_source=str(merged.get("orientation_source", "unset")),
                neutral_site=bool(merged.get("neutral_site", False)),
                active_from=merged.get("active_from"),
                active_through=merged.get("active_through"),
                notes=str(merged.get("notes", "")),
            )
        )
    return parks


def _bearing(value: Any, slug: str) -> float | None:
    """Validate a bearing at load time, so a typo fails on startup not at price time."""
    if value is None:
        return None
    try:
        bearing = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"park '{slug}': cf_bearing_deg {value!r} is not a number") from exc
    if not 0.0 <= bearing < 360.0:
        raise ValueError(
            f"park '{slug}': cf_bearing_deg {bearing} is outside [0, 360). "
            "Bearings are degrees clockwise from true north."
        )
    return bearing


def active_parks(settings: Settings, season: int) -> list[Park]:
    return [park for park in load_parks(settings) if park.is_active(season)]


def by_slug(settings: Settings, slug: str) -> Park:
    for park in load_parks(settings):
        if park.slug == slug:
            return park
    raise KeyError(f"no park with slug '{slug}'")


def require_orientation(park: Park) -> float:
    """The park's centre-field bearing, or a loud failure."""
    if park.cf_bearing_deg is None:
        raise OrientationUnknown(
            f"park '{park.slug}' has no cf_bearing_deg. A wind direction cannot be "
            "resolved into an out-to-centre component without it, and guessing "
            "would produce a confidently wrong feature rather than a missing one. "
            "Measure it and set it in config/parks.yaml."
        )
    return park.cf_bearing_deg


def orientation_coverage(settings: Settings, season: int) -> tuple[list[Park], list[Park]]:
    """``(with_bearing, without_bearing)`` among active parks for ``season``."""
    active = active_parks(settings, season)
    return (
        [p for p in active if p.has_orientation],
        [p for p in active if not p.has_orientation],
    )
