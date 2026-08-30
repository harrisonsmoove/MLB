"""Hydrate rejection: bisecting it, and surviving it.

StatsAPI answers an unrecognised hydrate term with 406 for the *whole* request.
It does not ignore the term. One stale term in a six-term string returned
nothing on every schedule fetch, which left the completeness check with no
denominator -- and would also have failed the first step of the backfill, since
both consumers sent the same string.

Offline throughout. The bisector takes a probe callable precisely so its logic
can be tested without reaching StatsAPI, which this container cannot do anyway.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mlb_edge.http import UpstreamError
from mlb_edge.hydrate import (
    HYDRATE_REJECTION_STATUSES,
    base_term,
    bisect_terms,
    hydrate_string,
    is_hydrate_rejection,
    strip_sub_hydrations,
)
from mlb_edge.ingest.base import FetchTask, IngestReport

REPO_ROOT = Path(__file__).resolve().parents[1]

TERMS = ["probablePitcher(note)", "linescore", "venue", "weather", "seriesStatus"]


def _probe_rejecting(*bad: str):
    """A probe that 406s any subset containing one of ``bad``."""

    def probe(subset: list[str]) -> tuple[bool, int | None, str]:
        if any(term in bad for term in subset):
            return False, 406, "HTTP 406 Not Acceptable"
        return True, 200, ""

    return probe


# --- finding the bad term --------------------------------------------------


def test_bisect_finds_the_single_rejected_term() -> None:
    result = bisect_terms(TERMS, _probe_rejecting("weather"))
    assert result.rejected == ["weather"]
    assert set(result.accepted) == set(TERMS) - {"weather"}
    assert "weather" in result.suggestion()


def test_bisect_finds_two_rejected_terms() -> None:
    """Linear, not binary, on purpose: a binary search reports one culprit.

    Six cheap unauthenticated requests is not worth trading for an answer that
    sends you back for a second round when two terms have gone stale.
    """
    result = bisect_terms(TERMS, _probe_rejecting("weather", "linescore"))
    assert sorted(result.rejected) == ["linescore", "weather"]


def test_all_terms_accepted_reports_no_change_needed() -> None:
    result = bisect_terms(TERMS, _probe_rejecting())
    assert result.rejected == []
    assert "accepted as configured" in result.suggestion()


def test_a_dead_connection_is_not_reported_as_a_hydrate_problem() -> None:
    """The bare call failing means the diagnosis is something else entirely."""

    def probe(_subset):
        return False, None, "ConnectError: no route to host"

    result = bisect_terms(TERMS, probe)
    assert not result.baseline_ok
    assert "not a hydrate problem" in result.suggestion()
    assert "no route to host" in result.suggestion()


def test_a_timeout_is_inconclusive_not_a_rejection() -> None:
    """A term must not be deleted from the config because the network blipped."""

    def probe(subset):
        if not subset:
            return True, 200, ""
        if "venue" in subset:
            return False, None, "ReadTimeout"
        return True, 200, ""

    result = bisect_terms(TERMS, probe)
    assert result.rejected == []
    assert result.inconclusive == ["venue"]
    assert "Re-run before" in result.suggestion()


def test_terms_that_pass_alone_but_fail_together_are_named() -> None:
    def probe(subset):
        return (len(subset) < 2, 406 if len(subset) >= 2 else 200, "")

    result = bisect_terms(TERMS, probe)
    assert result.combined_ok is False
    assert result.rejected == []


def test_probe_is_called_once_per_term_plus_baseline_and_combined() -> None:
    calls: list[list[str]] = []

    def probe(subset):
        calls.append(list(subset))
        return True, 200, ""

    bisect_terms(TERMS, probe)
    assert calls[0] == []
    assert [c for c in calls[1 : 1 + len(TERMS)]] == [[t] for t in TERMS]
    assert calls[-1] == TERMS


def test_combined_check_is_skipped_when_a_term_already_failed() -> None:
    """No point asking a question whose answer is already known."""
    calls: list[list[str]] = []

    def probe(subset):
        calls.append(list(subset))
        return ("weather" not in subset, 406 if "weather" in subset else 200, "")

    bisect_terms(TERMS, probe)
    assert calls[-1] != TERMS


# --- the rejection statuses ------------------------------------------------


@pytest.mark.parametrize("status", sorted(HYDRATE_REJECTION_STATUSES))
def test_rejection_statuses_trigger_the_fallback(status: int) -> None:
    assert is_hydrate_rejection(status)


@pytest.mark.parametrize("status", [200, 429, 500, 503, None])
def test_other_failures_do_not_trigger_the_fallback(status: int | None) -> None:
    """A 500 or a rate limit must retry the real request, not silently reduce it."""
    assert not is_hydrate_rejection(status)


def test_hydrate_string_preserves_order() -> None:
    assert hydrate_string(TERMS).startswith("probablePitcher(note),linescore")
    assert hydrate_string([]) == ""


# --- the ingester degrades rather than losing the range --------------------


def test_schedule_tasks_declare_a_fallback_chain(settings) -> None:
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester

    ingester = MlbScheduleIngester(settings)
    tasks = ingester.plan(date(2026, 8, 1), date(2026, 8, 14))

    assert tasks
    for task in tasks:
        assert "hydrate=" in task.url
        assert task.fallbacks
        # The last resort carries no hydrate at all, and names the whole loss.
        assert "hydrate" not in task.fallbacks[-1].url
        # The gap must be named, not implied. Nulls in probable_pitchers read as
        # "no starter announced" unless something says otherwise.
        assert "ABSENT" in task.fallbacks[-1].note
        assert "probe-hydrate" in task.fallbacks[-1].note


def test_a_degraded_task_is_reported_as_degraded_not_clean() -> None:
    report = IngestReport(source="mlb_statsapi", tasks_planned=1, tasks_fetched=1)
    assert "DEGRADED" not in report.summary()

    report.degraded.append("schedule/2026-08-01_2026-08-07: HTTP 406")
    assert "DEGRADED=1" in report.summary()
    assert report.failures == []


def test_fetch_task_defaults_to_no_fallback() -> None:
    """Only a task that declares one may be reduced. No implicit degrading."""
    task = FetchTask(dataset="d", partition="p", url="u")
    assert task.fallbacks == ()


# --- the degrade actually happens, end to end ------------------------------


class _HydrateRejectingClient:
    """406s anything carrying a hydrate parameter, 200s the bare call.

    Which is exactly what StatsAPI did on the box.
    """

    def __init__(self, payload: bytes, *, status: int = 406):
        self.payload = payload
        self.status = status
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "hydrate=" in url:
            raise UpstreamError(f"HTTP {self.status}", status=self.status, url=url)

        outer = self

        class _Response:
            status = 200
            headers: dict[str, str] = {}
            content = outer.payload
            content_type = "application/json"

            def __init__(self_inner) -> None:
                self_inner.url = url

            def text(self_inner) -> str:
                return outer.payload.decode()

        return _Response()


_SCHEDULE_BODY = json.dumps(
    {
        "dates": [
            {
                "date": "2026-08-01",
                "games": [
                    {
                        "gamePk": 745001,
                        "gameType": "R",
                        "gameDate": "2026-08-01T23:05:00Z",
                        "teams": {
                            "home": {"team": {"id": 147, "name": "New York Yankees"}},
                            "away": {"team": {"id": 111, "name": "Boston Red Sox"}},
                        },
                    }
                ],
            }
        ],
        "totalGames": 1,
    }
).encode()


def test_a_406_degrades_to_the_bare_call_and_still_loads_games(
    settings, tmp_path
) -> None:
    """The failure that shipped: one stale hydrate term, zero games ingested.

    The games spine is what step (a) of the backfill needs. Losing it to a
    hydrate term is losing the whole range for the sake of the extras.
    """
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester
    from mlb_edge.storage.rawcache import RawCache

    client = _HydrateRejectingClient(_SCHEDULE_BODY)
    ingester = MlbScheduleIngester(
        settings, cache=RawCache(tmp_path / "raw"), client=client
    )
    report = ingester.run(date(2026, 8, 1), date(2026, 8, 1))

    assert report.failures == []
    assert report.degraded, "a reduced request must be reported as degraded"
    assert report.tasks_fetched == 1
    assert any("hydrate=" in u for u in client.urls)
    assert any("hydrate" not in u for u in client.urls)


def test_the_degrade_announces_itself(settings, tmp_path, capsys) -> None:
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester
    from mlb_edge.storage.rawcache import RawCache

    ingester = MlbScheduleIngester(
        settings,
        cache=RawCache(tmp_path / "raw"),
        client=_HydrateRejectingClient(_SCHEDULE_BODY),
    )
    ingester.run(date(2026, 8, 1), date(2026, 8, 1))

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "406" in out
    assert "ABSENT" in out


def test_a_500_does_not_silently_reduce_the_request(settings, tmp_path) -> None:
    """Only a hydrate rejection may degrade.

    Reducing on any error would turn a transient upstream failure into a
    permanent silent loss of probable pitchers.
    """
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester
    from mlb_edge.storage.rawcache import RawCache

    client = _HydrateRejectingClient(_SCHEDULE_BODY, status=500)
    ingester = MlbScheduleIngester(
        settings, cache=RawCache(tmp_path / "raw"), client=client
    )
    report = ingester.run(date(2026, 8, 1), date(2026, 8, 1))

    assert report.failures
    assert report.degraded == []
    assert all("hydrate=" in u for u in client.urls)


# --- the distinction that nearly cost the starters -------------------------


def test_a_rejected_sub_hydration_is_not_a_rejected_term() -> None:
    """Measured live: probablePitcher(note) 406, probablePitcher 200.

    Reporting the first as "probablePitcher rejected" would have dropped the
    probable starters from the config over a sub-field. The simulator keys on
    the starter matchup, and a missing starter reads as "not announced yet"
    rather than as a bug, so nothing downstream would have caught it.
    """
    live = {"probablePitcher": 200, "linescore": 200, "venue": 406, "weather": 406}

    def probe(subset):
        if not subset:
            return True, 200, ""
        status = live.get(subset[0], 406)
        return status == 200, status, f"HTTP {status}"

    result = bisect_terms(["probablePitcher(note)", "linescore", "venue"], probe)

    assert [t.term for t in result.stripped] == ["probablePitcher(note)"]
    assert result.rejected == ["venue"]
    assert "probablePitcher" not in result.rejected
    assert result.corrected_terms() == ["probablePitcher", "linescore"]
    assert "keep the term, drop the sub-hydration" in result.suggestion()


def test_a_term_with_no_sub_hydration_is_not_probed_twice() -> None:
    calls: list[list[str]] = []

    def probe(subset):
        calls.append(list(subset))
        return (subset != ["venue"], 406 if subset == ["venue"] else 200, "")

    bisect_terms(["venue"], probe)
    assert calls == [[], ["venue"]]


def test_base_probe_only_runs_on_a_rejection_not_a_timeout() -> None:
    """A network blip must not be investigated as if it were a hydrate verdict."""
    calls: list[list[str]] = []

    def probe(subset):
        calls.append(list(subset))
        if subset == ["probablePitcher(note)"]:
            return False, None, "ReadTimeout"
        return True, 200, ""

    result = bisect_terms(["probablePitcher(note)"], probe)
    assert calls == [[], ["probablePitcher(note)"]]
    assert result.inconclusive == ["probablePitcher(note)"]
    assert result.rejected == []


def test_base_term_strips_only_the_first_paren() -> None:
    assert base_term("game(content(summary))") == "game"
    assert base_term("probablePitcher(note)") == "probablePitcher"
    assert base_term("linescore") == "linescore"


def test_strip_sub_hydrations_dedupes_and_keeps_order() -> None:
    assert strip_sub_hydrations(["probablePitcher(note)", "probablePitcher", "linescore"]) == [
        "probablePitcher",
        "linescore",
    ]


# --- the chain stops at the first step that works --------------------------


class _SubHydrationRejectingClient:
    """406s only requests carrying a parenthesised hydrate term.

    Which is what the live API did: probablePitcher(note) 406, probablePitcher
    200.
    """

    def __init__(self, payload: bytes):
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "%28" in url or "(" in url:
            raise UpstreamError("HTTP 406", status=406, url=url)
        outer = self

        class _Response:
            status = 200
            headers: dict[str, str] = {}
            content = outer.payload
            content_type = "application/json"

            def __init__(self_inner) -> None:
                self_inner.url = url

            def text(self_inner) -> str:
                return outer.payload.decode()

        return _Response()


def test_a_rejected_sub_hydration_keeps_the_base_terms(settings, tmp_path) -> None:
    """The chain must stop at the first step that works, not fall to the floor.

    Dropping straight to the unhydrated call over a rejected sub-field is
    exactly the over-correction the bisector's old verdict would have caused.
    """
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester
    from mlb_edge.storage.rawcache import RawCache

    settings.source("mlb_statsapi").raw["schedule_hydrate"] = [
        "probablePitcher(note)",
        "linescore",
    ]
    client = _SubHydrationRejectingClient(_SCHEDULE_BODY)
    ingester = MlbScheduleIngester(
        settings, cache=RawCache(tmp_path / "raw"), client=client
    )
    report = ingester.run(date(2026, 8, 1), date(2026, 8, 1))

    assert report.failures == []
    assert len(report.degraded) == 1
    assert "sub-hydrations dropped" in report.degraded[0]
    # It settled on a hydrated request, not the bare one.
    assert "hydrate=probablePitcher%2Clinescore" in client.urls[-1] or (
        "hydrate=probablePitcher,linescore" in client.urls[-1]
    )
    assert "ABSENT" not in report.degraded[0]


def test_the_chain_falls_all_the_way_when_it_has_to(settings, tmp_path) -> None:
    from mlb_edge.ingest.mlb_statsapi import MlbScheduleIngester
    from mlb_edge.storage.rawcache import RawCache

    settings.source("mlb_statsapi").raw["schedule_hydrate"] = [
        "probablePitcher(note)",
        "linescore",
    ]
    client = _HydrateRejectingClient(_SCHEDULE_BODY)
    ingester = MlbScheduleIngester(
        settings, cache=RawCache(tmp_path / "raw"), client=client
    )
    report = ingester.run(date(2026, 8, 1), date(2026, 8, 1))

    assert report.failures == []
    assert "ABSENT" in report.degraded[0]
    assert "hydrate" not in client.urls[-1]


# --- deployment-local config survives the deploy ---------------------------


def test_local_overlay_wins_at_the_leaves(tmp_path, monkeypatch) -> None:
    """`deploy.sh` runs `git reset --hard`, which reverted hand-edited flags in
    settings.yaml on every deploy. Splitting the two means shipped config
    changes still land while box-specific ones survive."""
    import shutil

    from mlb_edge.config import load_settings

    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    for name in ("settings.yaml", "parks.yaml", "books.yaml"):
        shutil.copy(REPO_ROOT / "config" / name, root / "config" / name)
    (root / "config" / "local.yaml").write_text(
        "sources:\n  odds:\n    enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    settings = load_settings(root)
    assert settings.source("odds").enabled is True
    # Untouched keys come through from the shipped file, not the overlay.
    assert settings.source("mlb_statsapi").get("schedule_hydrate") == [
        "probablePitcher",
        "linescore",
    ]


def test_local_overlay_announces_what_it_changed(tmp_path, capsys, monkeypatch) -> None:
    """A file that quietly changes which sources are enabled is exactly what a
    future debugging session would not think to look for."""
    import shutil

    from mlb_edge import config as config_module
    from mlb_edge.config import load_settings

    root = tmp_path / "app2"
    (root / "config").mkdir(parents=True)
    for name in ("settings.yaml", "parks.yaml", "books.yaml"):
        shutil.copy(REPO_ROOT / "config" / name, root / "config" / name)
    (root / "config" / "local.yaml").write_text(
        "sources:\n  odds:\n    enabled: true\n", encoding="utf-8"
    )
    config_module._announced_overlays.clear()
    monkeypatch.setenv("ODDS_API_KEY", "test-key")

    load_settings(root)
    out = capsys.readouterr().out
    assert "local.yaml" in out
    assert "sources.odds.enabled" in out


def test_no_overlay_file_is_silent(tmp_path, capsys) -> None:
    import shutil

    from mlb_edge.config import load_settings

    root = tmp_path / "app3"
    (root / "config").mkdir(parents=True)
    for name in ("settings.yaml", "parks.yaml", "books.yaml"):
        shutil.copy(REPO_ROOT / "config" / name, root / "config" / name)

    load_settings(root)
    assert "local.yaml" not in capsys.readouterr().out


def test_deep_merge_does_not_clobber_sibling_keys() -> None:
    from mlb_edge.config import _deep_merge

    base = {"sources": {"odds": {"enabled": False, "tier": 0}, "kalshi": {"enabled": False}}}
    merged, changed = _deep_merge(base, {"sources": {"odds": {"enabled": True}}})

    assert merged["sources"]["odds"] == {"enabled": True, "tier": 0}
    assert merged["sources"]["kalshi"] == {"enabled": False}
    assert changed == ["sources.odds.enabled"]


def test_deep_merge_reports_nothing_when_the_overlay_agrees() -> None:
    """Restating a shipped value is not an override and should not be announced."""
    from mlb_edge.config import _deep_merge

    _, changed = _deep_merge({"a": {"b": 1}}, {"a": {"b": 1}})
    assert changed == []


def test_local_config_is_gitignored() -> None:
    """Pinned: committing it would ship one box's flags to every other."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/local.yaml" in ignored
