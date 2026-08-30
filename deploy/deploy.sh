#!/usr/bin/env bash
#
# Deploy the mlb-edge poller to a Debian/Ubuntu box with systemd.
#
#   sudo ./deploy/deploy.sh
#
# Idempotent: safe to re-run for every deploy. It will never overwrite an
# existing secrets file, and it restarts the poller last so a failed build
# leaves the currently-running poller alone rather than taking the archive down
# to install something broken.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mlb-edge}"
ENV_DIR="${ENV_DIR:-/etc/mlb-edge}"
ENV_FILE="${ENV_DIR}/mlb-edge.env"
SERVICE_USER="${SERVICE_USER:-mlbedge}"
# Source of the code. Normally the GitHub remote, but a **git bundle file path
# also works** -- `git clone` and `git fetch` both accept one. That matters when
# the branch has not reached GitHub yet:
#
#   sudo REPO_URL=/root/mlb-edge.bundle ./deploy/deploy.sh
#
# Keep the bundle on disk if you use it; the update path fetches from the same
# location on every subsequent deploy.
REPO_URL="${REPO_URL:-https://github.com/harrisonsmoove/MLB}"
BRANCH="${BRANCH:-claude/mlb-simulation-betting-txwq10}"
UNIT_DIR=/etc/systemd/system

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. service user -------------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating service user ${SERVICE_USER}"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
else
  log "service user ${SERVICE_USER} exists"
fi

# --- 2. uv -----------------------------------------------------------------
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  UV_BIN="$(command -v uv)" || die "uv install failed"
fi
log "uv: ${UV_BIN} ($("$UV_BIN" --version))"

# --- 3. source -------------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
  log "updating ${APP_DIR} (${BRANCH})"
  # `reset --hard` below discards working-tree changes. Tracked config edited by
  # hand on the box is real work, and losing it silently on every deploy is how
  # the same two enable flags got re-flipped three times with the line numbers
  # moving each run. Preserve it and say where it went; the durable place for
  # such edits is config/local.yaml, which is git-ignored and never reset.
  DIRTY_CONFIG="$(git -C "$APP_DIR" status --porcelain -- config/ 2>/dev/null || true)"
  if [[ -n "$DIRTY_CONFIG" ]]; then
    BACKUP_DIR="$APP_DIR/data/config-backups/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$BACKUP_DIR"
    git -C "$APP_DIR" diff -- config/ > "$BACKUP_DIR/config.diff" 2>/dev/null || true
    while read -r _ file; do
      [[ -f "$APP_DIR/$file" ]] && cp "$APP_DIR/$file" "$BACKUP_DIR/$(basename "$file")"
    done <<< "$DIRTY_CONFIG"
    warn "config/ had uncommitted edits; they are about to be reset."
    warn "  saved to ${BACKUP_DIR}"
    warn "  move them into ${APP_DIR}/config/local.yaml so they survive the next deploy"
  fi
  git -C "$APP_DIR" fetch --depth 50 origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/${BRANCH}"
elif [[ -d "$APP_DIR" && -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
  die "${APP_DIR} exists and is not a git checkout; refusing to overwrite it"
else
  log "cloning ${REPO_URL} -> ${APP_DIR}"
  git clone --depth 50 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
log "at commit $(git -C "$APP_DIR" rev-parse --short HEAD)"

# The script you invoked is not necessarily the script this commit ships. Deploy
# is normally run from a clone at /tmp/mlb-edge made on some earlier day, so
# every fix to deploy.sh would otherwise take effect one deploy late -- which is
# exactly how the config/local.yaml step shipped, ran nothing, and logged
# nothing. Hand over to the version that was just checked out.
#
# It also removes a quieter hazard: bash reads a script incrementally, so when
# deploy is run from $APP_DIR the `reset --hard` above rewrites the file under
# the interpreter mid-run and execution resumes at a byte offset into different
# content. That failure has no error message at all.
DEPLOYED_SELF="$APP_DIR/deploy/$(basename "$0")"
if [[ -z "${MLB_EDGE_DEPLOY_REEXEC:-}" && -f "$DEPLOYED_SELF" ]]; then
  if ! cmp -s "$0" "$DEPLOYED_SELF"; then
    log "deploy script differs from the one at this commit; re-executing $DEPLOYED_SELF"
    export MLB_EDGE_DEPLOY_REEXEC=1
    exec bash "$DEPLOYED_SELF" "$@"
  fi
  log "deploy script matches this commit"
else
  [[ -n "${MLB_EDGE_DEPLOY_REEXEC:-}" ]] && log "running the checked-out deploy script"
fi

# A bundle has no branch tracking, so make sure the checkout is actually on the
# branch rather than detached at whatever the bundle's HEAD happened to be.
git -C "$APP_DIR" symbolic-ref -q HEAD >/dev/null || \
  git -C "$APP_DIR" checkout -q -B "$BRANCH" FETCH_HEAD 2>/dev/null || true

# --- 4. dependencies -------------------------------------------------------
log "installing dependencies"
(cd "$APP_DIR" && "$UV_BIN" sync --no-dev)
[[ -x "$APP_DIR/.venv/bin/mlb-edge" ]] || die "build produced no mlb-edge binary"

# --- 5. secrets ------------------------------------------------------------
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  log "creating ${ENV_FILE} from the example"
  install -m 640 "$SCRIPT_DIR/mlb-edge.env.example" "$ENV_FILE"
  chown root:"$SERVICE_USER" "$ENV_FILE"
  warn "${ENV_FILE} has no credentials yet. Fill it in, then re-run this script."
  NEEDS_SECRETS=1
else
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  NEEDS_SECRETS=0
  # shellcheck disable=SC1090
  set +u; source "$ENV_FILE"; set -u
  [[ -n "${ODDS_API_KEY:-}"     ]] || warn "ODDS_API_KEY is empty; the odds poller will not start"
  [[ -n "${KALSHI_API_KEY_ID:-}" ]] || warn "KALSHI_API_KEY_ID is empty; Kalshi polling may be limited"
  [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || warn "TELEGRAM_BOT_TOKEN is empty; alerts go to the journal only"
  grep -q '^\s*push_command:\s*""' "$APP_DIR/config/settings.yaml" 2>/dev/null && \
    warn "backup.push_command is empty; backups stay on this box and will not survive it"
fi

# --- 5b. deployment-local config -------------------------------------------
# Written once and never touched again, so `git reset --hard` above cannot
# revert it. Which sources to enable is derived from which credentials exist
# rather than guessed: a source enabled without its key fails config validation
# at startup, which is correct but unhelpful to arrive at by default.
LOCAL_CONFIG="$APP_DIR/config/local.yaml"
log "local config: $LOCAL_CONFIG"
if [[ -f "$LOCAL_CONFIG" ]]; then
  log "  found existing -- deploy never overwrites it. Enabled sources:"
  grep -n "enabled" "$LOCAL_CONFIG" 2>/dev/null | sed 's/^/    /' || log "    (no enabled: lines)"
elif [[ "$NEEDS_SECRETS" -eq 1 ]]; then
  # Nothing to derive the flags from yet. Say so rather than writing a file of
  # falses that then looks deliberate on the next run.
  warn "  NOT created: ${ENV_FILE} has no credentials yet."
  warn "  Fill it in and re-run deploy; this file is written from what it contains."
else
  ODDS_ENABLED=false; [[ -n "${ODDS_API_KEY:-}" ]] && ODDS_ENABLED=true
  KALSHI_ENABLED=false; [[ -n "${KALSHI_API_KEY_ID:-}" ]] && KALSHI_ENABLED=true
  cat > "$LOCAL_CONFIG" <<LOCALEOF
# Deployment-local overrides, layered over config/settings.yaml at load time.
#
# This file is git-ignored and deploy.sh never rewrites it, so edits here
# survive the \`git reset --hard\` that every deploy performs. Put anything
# box-specific here rather than editing settings.yaml, which is shipped.
#
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ) from the credentials present in
# ${ENV_FILE}. Change freely.
sources:
  odds:
    enabled: ${ODDS_ENABLED}
  kalshi:
    enabled: ${KALSHI_ENABLED}
LOCALEOF
  chown "$SERVICE_USER":"$SERVICE_USER" "$LOCAL_CONFIG"
  log "wrote $LOCAL_CONFIG (odds=${ODDS_ENABLED}, kalshi=${KALSHI_ENABLED})"
fi

# --- 6. data directories ---------------------------------------------------
log "preparing data directories"
mkdir -p "$APP_DIR/data/poll" "$APP_DIR/data/raw" "$APP_DIR/data/warehouse"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/data"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/mlb-edge" init --root "$APP_DIR" >/dev/null
log "warehouse initialised"

# --- 7. units --------------------------------------------------------------
log "installing systemd units"
for unit in mlb-edge-poller.service \
            mlb-edge-refresh.service mlb-edge-refresh.timer \
            mlb-edge-import.service mlb-edge-import.timer \
            mlb-edge-backup.service mlb-edge-backup.timer; do
  install -m 644 "$SCRIPT_DIR/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload

# --- 7b. did the deploy actually achieve anything? -------------------------
# The same question the completeness check asks of the poller, asked of the
# deploy. Everything above can print green while the result is a service that
# starts, finds nothing enabled and restarts forever. Ask the config directly.
if [[ "$NEEDS_SECRETS" -eq 0 ]]; then
  log "verifying resolved configuration"
  ENABLED_OUT="$(sudo -u "$SERVICE_USER" env \
      ODDS_API_KEY="${ODDS_API_KEY:-}" \
      KALSHI_API_KEY_ID="${KALSHI_API_KEY_ID:-}" \
      KALSHI_PRIVATE_KEY_PATH="${KALSHI_PRIVATE_KEY_PATH:-}" \
      "$APP_DIR/.venv/bin/mlb-edge" poll-sources --root "$APP_DIR" 2>&1)" || true
  printf '%s\n' "$ENABLED_OUT" | sed 's/^/    /'
  if ! printf '%s' "$ENABLED_OUT" | grep -q 'enabled: '; then
    warn "no pollable source resolved as enabled."
    warn "  The service will start, find nothing to poll and restart on a loop."
    warn "  Edit ${LOCAL_CONFIG} and re-run, or check the credentials in ${ENV_FILE}."
  fi
fi

if [[ "$NEEDS_SECRETS" -eq 1 ]]; then
  warn "units installed but NOT started -- add credentials to ${ENV_FILE} and re-run"
  exit 0
fi

# --- 8. start --------------------------------------------------------------
log "enabling and starting services"
systemctl enable --now mlb-edge-poller.service
systemctl enable --now mlb-edge-refresh.timer
systemctl enable --now mlb-edge-import.timer
systemctl enable --now mlb-edge-backup.timer
# The poller is restarted last and explicitly, so a deploy that got this far is
# the version now running.
systemctl restart mlb-edge-poller.service

sleep 3
log "status"
systemctl --no-pager --lines=0 status mlb-edge-poller.service || true

cat <<EOF

Deployed. Useful next:

  journalctl -u mlb-edge-poller -f          # watch it poll
  $APP_DIR/.venv/bin/mlb-edge poll-status --root $APP_DIR
  $APP_DIR/.venv/bin/mlb-edge verify --root $APP_DIR

If poll-status reports the odds poller throttled, that is the free-tier budget
working as designed -- see deploy/README.md.
EOF
