"""Alert configuration and diagnostics.

Alerting that is discovered to be broken during the first outage has already
failed at its job. These tests cover the setup path: what happens when
credentials are missing, wrong, or half-present, and whether the failure says
which of those it is.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from mlb_edge.alerting import (
    Alert,
    CompositeAlerter,
    LogAlerter,
    Severity,
    TelegramAlerter,
    _diagnose,
    build_alerter,
)


# ---------------------------------------------------------------------------
# Standing rule: a degraded path announces itself
# ---------------------------------------------------------------------------
def test_missing_credentials_warn_rather_than_degrading_quietly(monkeypatch, capsys):
    """Journal-only alerting reaches nobody at 3am, so it cannot be silent."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    alerter = build_alerter()
    output = capsys.readouterr().out

    assert "WARN" in output
    assert "journal only" in output
    assert len(alerter.backends) == 1, "only the log backend should be present"


def test_half_configured_credentials_also_warn(monkeypatch, capsys):
    """A token with no chat id is a misconfiguration, not a working setup."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    build_alerter()
    assert "WARN" in capsys.readouterr().out


def test_both_credentials_present_adds_the_backend(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnop")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")

    alerter = build_alerter()
    assert len(alerter.backends) == 2
    assert "WARN" not in capsys.readouterr().out


def test_describe_masks_the_token(monkeypatch):
    """The description is printed and logged; it must not leak the secret."""
    telegram = TelegramAlerter(token="123456:SUPERSECRETTOKENVALUE", chat_id="987")
    description = telegram.describe()
    assert "SUPERSECRETTOKENVALUE" not in description
    assert "987" in description


# ---------------------------------------------------------------------------
# Diagnostics point at the thing to fix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,description,expected",
    [
        (401, "Unauthorized", "TELEGRAM_BOT_TOKEN is wrong"),
        (400, "Bad Request: chat not found", "TELEGRAM_CHAT_ID is wrong"),
        (403, "Forbidden: bot was blocked by the user", "blocked"),
        (500, "Internal Server Error", "HTTP 500"),
    ],
)
def test_diagnosis_names_the_actual_problem(status, description, expected):
    """"Delivery failed" is not actionable at 3am; the cause is."""
    assert expected in _diagnose(status, description)


def test_chat_not_found_points_at_the_discovery_command():
    assert "discover-chat" in _diagnose(400, "Bad Request: chat not found")


def test_delivery_failure_records_the_diagnosis(monkeypatch):
    telegram = TelegramAlerter(token="t", chat_id="c")

    def _raise(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="u", code=401, msg="Unauthorized", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    assert telegram.send(Alert(key="k", severity=Severity.INFO, subject="s")) is False
    assert "TELEGRAM_BOT_TOKEN is wrong" in telegram.last_error


def test_a_network_failure_is_reported_not_raised(monkeypatch):
    telegram = TelegramAlerter(token="t", chat_id="c")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no route"))
    )
    assert telegram.send(Alert(key="k", severity=Severity.INFO, subject="s")) is False
    assert "could not reach Telegram" in telegram.last_error


# ---------------------------------------------------------------------------
# Chat id discovery
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_discovery_lists_chats_that_messaged_the_bot(monkeypatch):
    payload = {
        "result": [
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "H"}}},
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "H"}}},
            {"message": {"chat": {"id": -222, "type": "group", "title": "Alerts"}}},
        ]
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(payload))

    chats, note = TelegramAlerter(token="t", chat_id="c").discover_chat_ids()
    assert {c["id"] for c in chats} == {111, -222}, "duplicates collapse"
    assert "2 chat(s)" in note


def test_discovery_explains_the_step_people_miss(monkeypatch):
    """Telegram will not reveal a chat id until the bot has been messaged."""
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _FakeResponse({"result": []})
    )
    chats, note = TelegramAlerter(token="t", chat_id="c").discover_chat_ids()
    assert chats == []
    assert "Message your bot once" in note


# ---------------------------------------------------------------------------
# Per-backend reporting
# ---------------------------------------------------------------------------
def test_per_backend_results_distinguish_partial_delivery(monkeypatch):
    """The journal accepting an alert does not mean a phone saw it."""

    class Failing:
        last_error = "telegram is down"

        def send(self, alert):
            return False

        def describe(self):
            return "telegram (token abc...wxyz, chat 1)"

    composite = CompositeAlerter(backends=[LogAlerter(), Failing()])
    results = composite.send_per_backend(
        Alert(key="k", severity=Severity.INFO, subject="s")
    )

    delivered = {description: ok for description, ok, _ in results}
    assert delivered["journal (always available)"] is True
    assert any(not ok and "telegram" in d for d, ok, _ in results)


def test_a_raising_backend_is_reported_not_propagated():
    class Exploding:
        def send(self, alert):
            raise RuntimeError("boom")

        def describe(self):
            return "exploding"

    results = CompositeAlerter(backends=[Exploding()]).send_per_backend(
        Alert(key="k", severity=Severity.INFO, subject="s")
    )
    assert results[0][1] is False
    assert "boom" in results[0][2]
