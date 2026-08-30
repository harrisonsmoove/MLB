"""Alert delivery and throttling.

A poller that dies at 3am and is not noticed until the weekend costs days of
closing lines that Tier 0 cannot backfill. So silence has to be actively
disproved rather than assumed to mean health.

Two rules shape this module:

* **The log backend always works.** Telegram needs a token, a network, and a
  third party to be up, and none of those are guaranteed at the moment something
  breaks. Every alert is written to stdout (journald) first, then delivered
  onward on a best-effort basis. A delivery failure is itself logged and never
  raises -- an alerter that can crash the poller has made the problem worse.
* **Repeats are throttled, not suppressed.** A venue that has been down for six
  hours should not send 24 identical messages, but it must not go quiet either,
  or the absence of new alerts reads as recovery. Repeats re-fire on a
  configurable interval.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mlb_edge.timeutil import ensure_utc, parse_iso_utc, utcnow


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    key: str
    """Stable identity for throttling. Same key means same ongoing problem."""

    severity: Severity
    subject: str
    body: str = ""

    def render(self) -> str:
        return f"[{self.severity}] {self.subject}" + (f"\n{self.body}" if self.body else "")


class Alerter(Protocol):
    def send(self, alert: Alert) -> bool: ...

    def describe(self) -> str: ...


class LogAlerter:
    """Writes to stdout. Under systemd that is journald, which is durable."""

    def send(self, alert: Alert) -> bool:
        print(f"[alert] {alert.render()}", flush=True)
        return True

    def describe(self) -> str:
        return "journal (always available)"


class TelegramAlerter:
    """Best-effort Telegram delivery.

    Never raises. A failure to deliver is logged and reported as False so the
    caller can note it, but the poller carries on -- losing an alert is bad,
    losing the archive because alerting broke is worse.
    """

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.last_error: str | None = None

    def describe(self) -> str:
        masked = f"{self.token[:8]}...{self.token[-4:]}" if len(self.token) > 14 else "set"
        return f"telegram (token {masked}, chat {self.chat_id})"

    def check(self) -> tuple[bool, str]:
        """Validate the token without sending anything, via getMe."""
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{self.token}/getMe", timeout=self.timeout
            ) as response:
                payload = json.loads(response.read())
            name = (payload.get("result") or {}).get("username", "?")
            return True, f"token valid, bot is @{name}"
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, "token rejected (401) -- TELEGRAM_BOT_TOKEN is wrong"
            return False, f"HTTP {exc.code} from getMe"
        except Exception as exc:  # noqa: BLE001
            return False, f"could not reach Telegram: {exc}"

    def discover_chat_ids(self) -> tuple[list[dict[str, Any]], str]:
        """Chat ids that have messaged this bot.

        Telegram will not tell a bot its own chat id until someone messages it
        first, which is the step people miss.
        """
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                timeout=self.timeout,
            ) as response:
                payload = json.loads(response.read())
        except Exception as exc:  # noqa: BLE001
            return [], f"could not reach Telegram: {exc}"

        chats: dict[Any, dict[str, Any]] = {}
        for update in payload.get("result", []) or []:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is not None:
                chats[chat["id"]] = {
                    "id": chat["id"],
                    "type": chat.get("type"),
                    "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                }
        if not chats:
            return [], (
                "no chats found. Message your bot once from the account you want "
                "alerts on, then run this again."
            )
        return list(chats.values()), f"{len(chats)} chat(s) found"

    @classmethod
    def from_env(cls) -> TelegramAlerter | None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return None
        return cls(token=token, chat_id=chat_id)

    def send(self, alert: Alert) -> bool:
        payload = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": alert.render(), "disable_web_page_preview": "true"}
        ).encode()
        request = urllib.request.Request(
            self.API.format(token=self.token),
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.last_error = None
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = json.loads(exc.read()).get("description", "")
            self.last_error = _diagnose(exc.code, body)
            print(f"[alert] telegram delivery failed: {self.last_error}", flush=True)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"could not reach Telegram: {exc}"
            print(f"[alert] telegram delivery failed: {self.last_error}", flush=True)
            return False


def _diagnose(status: int, description: str) -> str:
    """Turn a Telegram error into the thing to actually go and fix."""
    text = (description or "").lower()
    if status == 401:
        return "token rejected (401): TELEGRAM_BOT_TOKEN is wrong"
    if "chat not found" in text:
        return (
            "chat not found: TELEGRAM_CHAT_ID is wrong, or you have not messaged "
            "the bot yet. Run: mlb-edge test-alert --discover-chat"
        )
    if "bot was blocked" in text:
        return "the bot is blocked by that chat; unblock it in Telegram"
    return f"HTTP {status}: {description or 'no detail'}"


@dataclass
class CompositeAlerter:
    """Fan out to every backend; success if any accepted it."""

    backends: list[Alerter] = field(default_factory=list)

    def send(self, alert: Alert) -> bool:
        results = []
        for backend in self.backends:
            try:
                results.append(bool(backend.send(alert)))
            except Exception as exc:  # noqa: BLE001
                print(f"[alert] backend {type(backend).__name__} raised: {exc}", flush=True)
                results.append(False)
        return any(results)

    def send_per_backend(self, alert: Alert) -> list[tuple[str, bool, str]]:
        """``(description, delivered, detail)`` for each backend."""
        results: list[tuple[str, bool, str]] = []
        for backend in self.backends:
            description = backend.describe()
            try:
                delivered = bool(backend.send(alert))
                detail = getattr(backend, "last_error", None) or ""
            except Exception as exc:  # noqa: BLE001
                delivered, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((description, delivered, detail))
        return results

    def describe(self) -> str:
        return ", ".join(b.describe() for b in self.backends)


def build_alerter(*, enable_telegram: bool = True) -> CompositeAlerter:
    backends: list[Alerter] = [LogAlerter()]
    if enable_telegram:
        telegram = TelegramAlerter.from_env()
        if telegram is not None:
            backends.append(telegram)
        else:
            # Standing rule: a degraded path announces itself. Journal-only
            # alerting means nothing reaches a phone at 3am, which is exactly
            # when it matters.
            print(
                "[alert] WARN: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; "
                "alerts go to the journal only and nobody will see them",
                flush=True,
            )
    return CompositeAlerter(backends=backends)


class AlertThrottle:
    """Rate-limits repeats of the same alert key, persisted across restarts.

    Persistence matters: a poller in a crash loop would otherwise re-alert on
    every start, and the resulting noise is indistinguishable from a flapping
    service.
    """

    def __init__(self, path: Path, repeat_after: timedelta = timedelta(hours=1)) -> None:
        self.path = Path(path)
        self.repeat_after = repeat_after
        self._last: dict[str, datetime] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self._last = {k: parse_iso_utc(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001 - a corrupt throttle file must not block alerts
            self._last = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.isoformat() for k, v in self._last.items()}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
        temporary.replace(self.path)

    def should_send(self, key: str, *, now: datetime | None = None) -> bool:
        reference = ensure_utc(now or utcnow())
        last = self._last.get(key)
        return last is None or (reference - last) >= self.repeat_after

    def record(self, key: str, *, now: datetime | None = None) -> None:
        self._last[key] = ensure_utc(now or utcnow())
        self._save()

    def clear(self, key: str) -> None:
        """Forget a key once the condition resolves, so recovery re-arms it."""
        if self._last.pop(key, None) is not None:
            self._save()


def send_throttled(
    alerter: Alerter,
    throttle: AlertThrottle,
    alert: Alert,
    *,
    now: datetime | None = None,
) -> bool:
    if not throttle.should_send(alert.key, now=now):
        return False
    delivered = alerter.send(alert)
    # Recorded whether or not delivery succeeded: a Telegram outage should not
    # turn into a retry storm against the journal.
    throttle.record(alert.key, now=now)
    return delivered
