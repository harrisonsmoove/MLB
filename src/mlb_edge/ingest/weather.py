"""Weather ingestion (Open-Meteo: free, no key).

The useful quantity is not a compass bearing. It is *how much wind is blowing
out to centre field* -- a 15 mph wind is worth roughly nothing at one park and a
run at another depending on which way the stadium points. Resolving a
meteorological wind direction into that park-relative component requires the
park's orientation, and this repo deliberately does not have it yet
(``cf_bearing_deg`` is null for every park in ``parks.yaml``).

So the component columns are computed when an orientation exists and left null
when it does not. A guessed bearing would produce a confidently wrong feature,
which costs more than a missing one: a missing feature is visibly missing, while
a wrong one silently reverses the sign of a real effect at half the parks.

Two endpoints, chosen by age of the date:
  * ``forecast`` -- future and recent hours, refreshed as first pitch nears.
  * ``archive``  -- ERA5 reanalysis, ~5 day lag, used for backfill.

``as_of_ts`` is the retrieval time (when the forecast was issued, near enough
and never earlier than the truth). ``valid_ts`` is the hour being described.
Conflating them would let a backtest read the actual 7pm conditions from a
9am decision point.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any

import polars as pl

from mlb_edge.ingest.base import FetchTask, Ingester, provenance_columns
from mlb_edge.storage.rawcache import RawEntry
from mlb_edge.timeutil import parse_iso_utc, utcnow

# ERA5 reanalysis lags real time; inside this window only the forecast endpoint
# has data.
ARCHIVE_LAG_DAYS = 6


class WeatherIngester(Ingester):
    source_name = "weather"
    writes_tables = ("weather_hourly",)

    def plan(self, start: date, end: date, **kwargs: Any) -> list[FetchTask]:
        if self.warehouse is None:
            raise RuntimeError("WeatherIngester.plan needs a warehouse to read venues from")

        today = kwargs.get("today") or utcnow().date()
        venues = self.warehouse.sql(
            """
            SELECT v.venue_id, any_value(v.latitude) AS latitude,
                   any_value(v.longitude) AS longitude, any_value(v.roof) AS roof,
                   min(g.game_date_local) AS first_game, max(g.game_date_local) AS last_game
            FROM games g
            JOIN (
                SELECT * FROM (
                    SELECT *, row_number() OVER (PARTITION BY venue_id ORDER BY as_of_ts DESC) rn
                    FROM venues
                ) WHERE rn = 1
            ) v ON v.venue_id = g.venue_id
            WHERE g.game_date_local BETWEEN ? AND ?
              AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
            GROUP BY v.venue_id
            ORDER BY v.venue_id
            """,
            [start, end],
        )

        hourly = ",".join(self.config.get("hourly_variables", []) or [])
        chunk_days = int(kwargs.get("chunk_days", 30))

        tasks: list[FetchTask] = []
        for row in venues.iter_rows(named=True):
            venue_id = int(row["venue_id"])
            window_start = max(start, row["first_game"])
            window_end = min(end, row["last_game"])
            cursor = window_start
            while cursor <= window_end:
                chunk_end = min(cursor + timedelta(days=chunk_days - 1), window_end)
                use_archive = (today - chunk_end).days > ARCHIVE_LAG_DAYS
                base = (
                    self.config.get("archive_url", "").rstrip("/")
                    if use_archive
                    else self.config.base_url
                )
                path = (self.config.get("endpoints") or {})[
                    "archive" if use_archive else "forecast"
                ]
                tasks.append(
                    FetchTask(
                        dataset="hourly_archive" if use_archive else "hourly_forecast",
                        partition=f"{venue_id}_{cursor.isoformat()}_{chunk_end.isoformat()}",
                        url=f"{base}{path}",
                        params={
                            "latitude": row["latitude"],
                            "longitude": row["longitude"],
                            "start_date": cursor.isoformat(),
                            "end_date": chunk_end.isoformat(),
                            "hourly": hourly,
                            "timezone": "UTC",
                            "temperature_unit": "fahrenheit",
                            "wind_speed_unit": "mph",
                        },
                        # Reanalysis is settled history; a forecast is not.
                        max_age_seconds=None if use_archive else 3600.0,
                        context={"venue_id": venue_id, "is_observation": use_archive},
                    )
                )
                cursor = chunk_end + timedelta(days=1)
        return tasks

    def parse(
        self, entry: RawEntry, payload: bytes, task: FetchTask | None = None
    ) -> dict[str, pl.DataFrame]:
        data = json.loads(payload)
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return {"weather_hourly": pl.DataFrame()}

        venue_id = int(entry.partition.split("_", 1)[0])
        is_observation = entry.dataset.endswith("archive")
        orientation = self._orientation(venue_id)
        prov = provenance_columns(entry, self.source_name)

        rows: list[dict[str, Any]] = []
        for index, stamp in enumerate(times):
            speed = _at(hourly, "wind_speed_10m", index)
            direction = _at(hourly, "wind_direction_10m", index)
            out_component, cross_component = wind_components(
                speed, direction, orientation["cf_bearing_deg"]
            )
            rows.append(
                {
                    "venue_id": venue_id,
                    "valid_ts": parse_iso_utc(f"{stamp}Z" if len(stamp) == 16 else stamp),
                    "is_observation": is_observation,
                    "temperature_f": _at(hourly, "temperature_2m", index),
                    "relative_humidity": _at(hourly, "relative_humidity_2m", index),
                    "surface_pressure_hpa": _at(hourly, "surface_pressure", index),
                    "wind_speed_mph": speed,
                    "wind_direction_deg": direction,
                    "wind_out_to_cf_mph": out_component,
                    "wind_cross_lf_rf_mph": cross_component,
                    "precipitation_prob": _at(hourly, "precipitation_probability", index),
                    "cloud_cover_pct": _at(hourly, "cloud_cover", index),
                    "roof_closed": orientation["roof_closed"],
                    "as_of_ts": entry.retrieved_ts,
                    **prov,
                }
            )
        return {"weather_hourly": pl.DataFrame(rows)}

    def _orientation(self, venue_id: int) -> dict[str, Any]:
        default = {"cf_bearing_deg": None, "roof_closed": None}
        if self.warehouse is None:
            return default
        row = self.warehouse.sql(
            "SELECT cf_bearing_deg, roof FROM venues WHERE venue_id = ? "
            "ORDER BY as_of_ts DESC LIMIT 1",
            [venue_id],
        )
        if row.is_empty():
            return default
        roof = row["roof"][0]
        return {
            "cf_bearing_deg": row["cf_bearing_deg"][0],
            # A fixed dome is always closed. A retractable roof's state is not
            # published by any source ingested here, so it stays null rather
            # than being assumed open -- assuming open at a retractable park on
            # a hot day is exactly backwards.
            "roof_closed": True if roof == "fixed" else (False if roof == "none" else None),
        }


def wind_components(
    speed_mph: float | None, from_direction_deg: float | None, cf_bearing_deg: float | None
) -> tuple[float | None, float | None]:
    """Resolve wind into park-relative components.

    Meteorological convention: ``from_direction_deg`` is the direction the wind
    blows *from*. Wind blowing out to centre therefore arrives from behind home
    plate, at bearing ``cf_bearing_deg + 180``.

    Returns ``(out_to_cf, cross_lf_to_rf)`` in mph. Positive ``out_to_cf`` is
    wind carrying the ball toward centre; positive cross is toward right field.
    Both are ``None`` when the park's orientation is unknown, which is currently
    every park -- see the module docstring.
    """
    if speed_mph is None or from_direction_deg is None or cf_bearing_deg is None:
        return None, None
    tailwind_source = (float(cf_bearing_deg) + 180.0) % 360.0
    offset = math.radians(float(from_direction_deg) - tailwind_source)
    return float(speed_mph) * math.cos(offset), float(speed_mph) * math.sin(offset)


def _at(hourly: dict[str, Any], key: str, index: int) -> float | None:
    series = hourly.get(key)
    if not isinstance(series, list) or index >= len(series):
        return None
    value = series[index]
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
