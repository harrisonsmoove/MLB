"""The deploy script's config handling, executed rather than eyeballed.

`config/local.yaml` was never created on the box: the deploy that shipped the
creation step ran the *previous* version of deploy.sh, because deploy is
normally invoked from a clone made on an earlier day. The step existed, ran
nothing, and logged nothing -- the same shape as every other bug this week, in
the fix for that shape.

So the shell gets executed here, not just syntax-checked. Reading a shell script
and believing it works is exactly what failed.

The blocks under test are extracted from deploy.sh and run against stub
`log`/`warn` functions, so no root, systemd or network is involved.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy" / "deploy.sh"

PRELUDE = """
set -euo pipefail
log()  { printf 'log %s\\n' "$*"; }
warn() { printf 'warn %s\\n' "$*"; }
die()  { printf 'die %s\\n' "$*" >&2; exit 1; }
"""


def _block(marker: str) -> str:
    """The deploy.sh section starting at ``marker``, up to the next section."""
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index(marker)
    rest = text[start + len(marker) :]
    match = re.search(r"^# --- \d", rest, re.M)
    return marker + (rest[: match.start()] if match else rest)


def _run(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", PRELUDE + script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
    )


def test_deploy_script_parses() -> None:
    result = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- local.yaml is created, and says so ------------------------------------


LOCAL_BLOCK = "# --- 5b. deployment-local config"


def test_local_config_is_created_when_absent(tmp_path) -> None:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    result = _run(
        f'APP_DIR="{app}"\nENV_FILE=/dev/null\nNEEDS_SECRETS=0\n'
        'SERVICE_USER="$(id -un)"\nODDS_API_KEY=k\nKALSHI_API_KEY_ID=k\n'
        + _block(LOCAL_BLOCK)
    )

    assert result.returncode == 0, result.stderr
    local = app / "config" / "local.yaml"
    assert local.is_file(), result.stdout + result.stderr
    body = local.read_text()
    assert "odds:" in body and "enabled: true" in body
    assert "wrote" in result.stdout


def test_creation_is_logged_even_when_it_does_not_happen(tmp_path) -> None:
    """A file that silently is not there is worse than one silently wrong."""
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    result = _run(
        f'APP_DIR="{app}"\nENV_FILE=/etc/mlb-edge/mlb-edge.env\nNEEDS_SECRETS=1\n'
        'SERVICE_USER="$(id -un)"\n' + _block(LOCAL_BLOCK)
    )

    assert result.returncode == 0, result.stderr
    assert not (app / "config" / "local.yaml").exists()
    # It must say what it did and why, on the path where it does nothing.
    assert "local config:" in result.stdout
    assert "NOT created" in result.stdout


def test_an_existing_local_config_is_never_overwritten(tmp_path) -> None:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    local = app / "config" / "local.yaml"
    local.write_text("sources:\n  odds:\n    enabled: true\n# hand-edited\n")

    result = _run(
        f'APP_DIR="{app}"\nENV_FILE=/dev/null\nNEEDS_SECRETS=0\n'
        'SERVICE_USER="$(id -un)"\nODDS_API_KEY=""\nKALSHI_API_KEY_ID=""\n'
        + _block(LOCAL_BLOCK)
    )

    assert result.returncode == 0, result.stderr
    assert "# hand-edited" in local.read_text()
    assert "found existing" in result.stdout


def test_the_flags_follow_the_credentials_that_exist(tmp_path) -> None:
    """Derived from what is present, not guessed.

    A source enabled without its key fails config validation at startup, which
    is correct and an unhelpful default to arrive at.
    """
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    result = _run(
        f'APP_DIR="{app}"\nENV_FILE=/dev/null\nNEEDS_SECRETS=0\n'
        'SERVICE_USER="$(id -un)"\nODDS_API_KEY=present\nKALSHI_API_KEY_ID=""\n'
        + _block(LOCAL_BLOCK)
    )

    assert result.returncode == 0, result.stderr
    body = (app / "config" / "local.yaml").read_text()
    odds = body.split("odds:")[1].split("kalshi:")[0]
    kalshi = body.split("kalshi:")[1]
    assert "enabled: true" in odds
    assert "enabled: false" in kalshi


def test_local_config_logs_on_every_path(tmp_path) -> None:
    """Created, found, or skipped -- all three say so. Silence is the bug."""
    block = _block(LOCAL_BLOCK)
    app = tmp_path / "a"
    (app / "config").mkdir(parents=True)
    common = f'APP_DIR="{app}"\nSERVICE_USER="$(id -un)"\n'

    created = _run(common + 'ENV_FILE=/dev/null\nNEEDS_SECRETS=0\nODDS_API_KEY=k\nKALSHI_API_KEY_ID=k\n' + block)
    found = _run(common + 'ENV_FILE=/dev/null\nNEEDS_SECRETS=0\nODDS_API_KEY=k\nKALSHI_API_KEY_ID=k\n' + block)

    for result in (created, found):
        assert "local config:" in result.stdout, result.stdout


# --- the re-exec that would have made the above run at all -----------------


def test_deploy_reexecs_the_checked_out_script() -> None:
    """Deploy is run from a clone made on an earlier day, so without this every
    fix to deploy.sh lands one deploy late -- which is what happened."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "MLB_EDGE_DEPLOY_REEXEC" in text
    assert "exec bash" in text
    # Guarded, or it re-executes itself forever.
    assert 'if [[ -z "${MLB_EDGE_DEPLOY_REEXEC:-}" ' in text


def test_reexec_happens_after_the_checkout() -> None:
    """Before the checkout there is nothing new to hand over to."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("reset --hard") < text.index("exec bash")


def test_reexec_reports_when_it_does_not_fire() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "deploy script matches this commit" in text


# --- the deploy checks it achieved something -------------------------------


def test_deploy_verifies_a_source_is_actually_enabled() -> None:
    """Everything can print green and still leave a service polling nothing."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "poll-sources" in text
    assert "restart on a loop" in text


def test_dirty_config_is_backed_up_before_the_reset() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("config-backups") < text.index('reset --hard "origin/${BRANCH}"')


@pytest.mark.parametrize(
    "phrase",
    [
        "local config:",
        "config-backups",
        "poll-sources",
        "MLB_EDGE_DEPLOY_REEXEC",
    ],
)
def test_required_deploy_behaviour_is_present(phrase: str) -> None:
    assert phrase in DEPLOY.read_text(encoding="utf-8")


# --- the README has to actually work ---------------------------------------


README = REPO_ROOT / "deploy" / "README.md"


def _documented_commands() -> list[str]:
    """Every `mlb-edge ...` invocation the README tells someone to run."""
    text = README.read_text(encoding="utf-8")
    found: list[str] = []
    for match in re.finditer(r"mlb-edge ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", text):
        found.append(match.group(1))
    return sorted(set(found))


def test_every_command_in_the_readme_exists() -> None:
    """`mlb-edge backup --root X` was in here and would have failed on first
    use -- `backup` is a command group, so it needs `backup create`.

    A runbook is only useful if the commands in it run. This checks them
    against the CLI's own help rather than against memory.
    """
    binary = str(REPO_ROOT / ".venv" / "bin" / "mlb-edge")
    top = subprocess.run([binary, "--help"], capture_output=True, text=True).stdout

    for command in _documented_commands():
        head, _, sub = command.partition(" ")
        assert head in top, f"README documents `mlb-edge {command}`, no such command"
        if not sub:
            continue
        group = subprocess.run(
            [binary, head, "--help"], capture_output=True, text=True
        ).stdout
        if "Commands" not in group:
            # Not a group -- the second token is an argument (`probe kalshi`),
            # and the CLI is the authority on whether it is valid, not this test.
            continue
        assert sub in group, f"README documents `mlb-edge {command}`, no such subcommand"


def test_the_readme_does_not_send_edits_to_settings_yaml() -> None:
    """Every deploy runs `git reset --hard`. Telling someone to edit the
    tracked config is telling them to lose the edit."""
    for line in README.read_text(encoding="utf-8").splitlines():
        if "EDITOR" not in line:
            continue
        command, _, _comment = line.partition("#")
        assert "settings.yaml" not in command, line


def test_deploy_never_greps_the_shipped_config_for_settings() -> None:
    """The shipped file is not the config the process runs on.

    deploy grepped settings.yaml for an empty push_command -- where it is
    always empty, because the real value lives in local.yaml -- and warned on
    every deploy of a correctly configured box. A warning that misdescribes
    reality trains you to skim past warnings, which is the one thing this
    project cannot afford.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "grep" in stripped and "settings.yaml" in stripped:
            raise AssertionError(f"deploy greps the shipped config: {line}")


def test_deploy_asks_the_cli_about_backups() -> None:
    """The CLI reads the merged config. Anything checking config must too."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "backup status" in text
