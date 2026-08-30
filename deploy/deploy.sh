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
