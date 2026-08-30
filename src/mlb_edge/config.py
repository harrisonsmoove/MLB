"""Configuration loading.

Every endpoint, field map, rate limit and fee policy lives in ``config/*.yaml``.
Nothing in this package may hardcode an upstream URL, response shape or fee
formula -- if you find yourself typing one into a ``.py`` file, it belongs here
instead.

Secrets are referenced as ``${ENV_VAR}`` in YAML and resolved from the process
environment at load time. A missing secret is only an error when the owning
source is enabled, so a fresh clone with no credentials can still run the
schema, parser and point-in-time test suites.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

# Sentinel for "this value referenced an environment variable that is not set".
# Kept distinct from None so that "configured but absent" and "not configured"
# are distinguishable.
MISSING_SECRET = "__MISSING_SECRET__"


class ConfigError(RuntimeError):
    """Raised when configuration is absent, malformed or internally inconsistent."""


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value.strip())
        if match:
            return os.environ.get(match.group(1), MISSING_SECRET)
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward looking for the ``config/`` directory."""
    current = (start or Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "settings.yaml").is_file():
            return candidate
    raise ConfigError(
        "Could not locate repo root (no config/settings.yaml found walking up "
        f"from {current}). Set MLB_EDGE_ROOT to point at it."
    )


@dataclass(frozen=True)
class SourceConfig:
    """One upstream data source."""

    name: str
    raw: dict[str, Any]

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", False))

    @property
    def base_url(self) -> str:
        url = self.raw.get("base_url")
        if not url:
            raise ConfigError(f"source '{self.name}' has no base_url")
        return str(url).rstrip("/")

    def endpoint(self, name: str, **params: Any) -> str:
        """Return a fully-qualified URL for a named endpoint template."""
        endpoints = self.raw.get("endpoints") or {}
        if name not in endpoints:
            raise ConfigError(
                f"source '{self.name}' has no endpoint '{name}'. "
                f"Known: {sorted(endpoints)}"
            )
        try:
            path = str(endpoints[name]).format(**params)
        except KeyError as exc:
            raise ConfigError(
                f"endpoint '{self.name}.{name}' needs parameter {exc} which was not supplied"
            ) from exc
        return f"{self.base_url}{path}"

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def require(self, key: str) -> Any:
        """Fetch a value, failing loudly if it is absent or an unresolved secret."""
        value = self.raw.get(key)
        if value is None:
            raise ConfigError(f"source '{self.name}' is missing required key '{key}'")
        if value == MISSING_SECRET:
            raise ConfigError(
                f"source '{self.name}' key '{key}' references an environment variable "
                "that is not set. Export it before enabling this source."
            )
        return value

    def has_secret(self, key: str) -> bool:
        return self.raw.get(key) not in (None, MISSING_SECRET)


@dataclass(frozen=True)
class Settings:
    root: Path
    raw: dict[str, Any]
    parks: dict[str, Any]
    books: dict[str, Any]

    # -- paths ---------------------------------------------------------------
    def _path(self, key: str) -> Path:
        rel = self.raw["paths"][key]
        path = Path(rel)
        return path if path.is_absolute() else self.root / path

    @property
    def raw_dir(self) -> Path:
        return self._path("raw")

    @property
    def warehouse_path(self) -> Path:
        return self._path("warehouse")

    @property
    def reports_dir(self) -> Path:
        return self._path("reports")

    # -- project -------------------------------------------------------------
    @property
    def timezone(self) -> str:
        return str(self.raw["project"]["timezone"])

    @property
    def seasons(self) -> list[int]:
        return [int(s) for s in self.raw["project"]["seasons"]]

    # -- sources -------------------------------------------------------------
    def source(self, name: str) -> SourceConfig:
        sources = self.raw.get("sources") or {}
        if name not in sources:
            raise ConfigError(f"unknown source '{name}'. Known: {sorted(sources)}")
        return SourceConfig(name=name, raw=sources[name])

    def source_names(self) -> list[str]:
        return sorted((self.raw.get("sources") or {}).keys())

    def enabled_sources(self) -> list[str]:
        return [n for n in self.source_names() if self.source(n).enabled]

    # -- books ---------------------------------------------------------------
    def book(self, key: str) -> dict[str, Any] | None:
        for entry in self.books.get("books", []):
            if entry.get("key") == key:
                return entry
        return None

    def consensus_weight(self, book_key: str) -> float:
        """Consensus weight for a book. Recreational books are always zero.

        The zero is structural, not a tuning choice: a recreational book's price
        is the thing we are trying to beat, so letting it inform our estimate of
        truth would be marking our own homework.
        """
        entry = self.book(book_key)
        if entry is None:
            return 0.0
        if entry.get("role") == "recreational":
            return 0.0
        weights = self.books.get("consensus", {}).get("weights", {})
        if entry.get("role") == "exchange":
            return float(weights.get("prediction_market", 0.0))
        return float(weights.get(book_key, 0.0))

    @property
    def executable_venues(self) -> list[str]:
        return list(self.books.get("executable_venues", []))

    # -- convenience ---------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        if not isinstance(value, dict):
            raise ConfigError(f"settings has no '{name}' section")
        return value


#: Deployment-local overrides, layered over settings.yaml. Git-ignored, and
#: deploy.sh never writes it after creating it once.
LOCAL_CONFIG_NAME = "local.yaml"

_announced_overlays: set[Path] = set()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any], prefix: str = "") -> tuple[dict[str, Any], list[str]]:
    """Overlay wins at the leaves. Returns the merged mapping and what changed."""
    merged = dict(base)
    changed: list[str] = []
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key], nested = _deep_merge(merged[key], value, prefix=f"{path}.")
            changed.extend(nested)
        else:
            if merged.get(key) != value:
                changed.append(path)
            merged[key] = value
    return merged, changed


def _apply_local_overlay(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """Layer ``config/local.yaml`` over the repo settings, and say what it changed.

    This exists because ``deploy.sh`` runs ``git reset --hard``, which reverted
    hand-edited flags in ``settings.yaml`` on every deploy -- silently, and with
    the line numbers moving each time. Splitting the two means shipped config
    changes still land while deployment-local ones survive.

    The override is announced. A file that quietly changes which sources are
    enabled is exactly the kind of thing a future debugging session would not
    think to look for; a single line naming the overridden paths costs nothing
    and removes the surprise.
    """
    if not path.is_file():
        return raw
    overlay = _read_yaml(path)
    merged, changed = _deep_merge(raw, overlay)
    if changed and path not in _announced_overlays:
        _announced_overlays.add(path)
        print(
            f"[config] {path} overrides {len(changed)} setting(s): "
            + ", ".join(sorted(changed)[:8])
            + (" ..." if len(changed) > 8 else ""),
            flush=True,
        )
    return merged


def load_settings(root: Path | None = None) -> Settings:
    """Load and validate configuration. Not cached -- tests build variants."""
    if root is None:
        env_root = os.environ.get("MLB_EDGE_ROOT")
        root = Path(env_root) if env_root else find_repo_root()
    root = Path(root).resolve()

    config_dir = root / "config"
    settings_raw = _read_yaml(config_dir / "settings.yaml")
    settings_raw = _apply_local_overlay(settings_raw, config_dir / LOCAL_CONFIG_NAME)
    parks_raw = _read_yaml(config_dir / "parks.yaml")
    books_raw = _read_yaml(config_dir / "books.yaml")

    settings = Settings(
        root=root,
        raw=_resolve_env(settings_raw),
        parks=parks_raw,
        books=books_raw,
    )
    _validate(settings)
    return settings


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ConfigError(f"config file {path} did not parse to a mapping")
    return loaded


def _validate(settings: Settings) -> None:
    for required in ("project", "paths", "http", "sources"):
        settings.section(required)

    if not settings.seasons:
        raise ConfigError("project.seasons is empty")

    # A source that is enabled must have every secret it declares resolved,
    # so that a missing key fails at startup rather than mid-backfill.
    for name in settings.enabled_sources():
        source = settings.source(name)
        for key, value in source.raw.items():
            if value == MISSING_SECRET:
                raise ConfigError(
                    f"source '{name}' is enabled but '{key}' resolves to an unset "
                    "environment variable"
                )

    # Recreational books must never carry consensus weight. Enforced rather
    # than trusted, because a stray weight here would quietly corrupt every
    # fair line the system computes.
    weights = settings.books.get("consensus", {}).get("weights", {})
    for entry in settings.books.get("books", []):
        if entry.get("role") == "recreational" and weights.get(entry["key"], 0.0):
            raise ConfigError(
                f"recreational book '{entry['key']}' has a non-zero consensus weight"
            )

    for venue in settings.executable_venues:
        entry = settings.book(venue)
        if entry is None:
            raise ConfigError(f"executable venue '{venue}' is not defined in books.yaml")
        if not entry.get("executable"):
            raise ConfigError(f"venue '{venue}' is listed executable but not flagged executable")


@cache
def default_settings() -> Settings:
    """Process-wide settings for CLI use. Tests should call load_settings()."""
    return load_settings()
