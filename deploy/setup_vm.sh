#!/usr/bin/env bash
#
# One-shot deployment of the Telegram bot + data pipeline to the GCP VM.
# (tested target: GCP e2-micro, Ubuntu 22.04/24.04 x86_64).
#
# Run this ON THE VM after you have SSH access. It is idempotent — safe to
# re-run after a `git pull` to redeploy.
#
# Usage:
#   git clone <your-repo-url> ~/polish-it-job-market-intelligence
#   cd ~/polish-it-job-market-intelligence
#   cp /path/to/your/.env .env      # bring your secrets over (chmod 600)
#   chmod +x deploy/setup_vm.sh
#   ./deploy/setup_vm.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="telegram-bot"
PIPELINE_SERVICE="pipeline"
RUN_USER="$(whoami)"

echo "==> Repo:    ${REPO_DIR}"
echo "==> User:    ${RUN_USER}"

# --- 1. System packages ------------------------------------------------------
echo "==> Installing system packages (python3, venv, git)..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv python3-pip git

# --- 2. Check for secrets ----------------------------------------------------
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "ERROR: ${REPO_DIR}/.env not found."
  echo "Copy your .env (TELEGRAM_BOT_TOKEN, ANALYTICS_SALT, ...) here first."
  echo "  scp .env  <vm-user>@<vm-ip>:${REPO_DIR}/.env"
  exit 1
fi
chmod 600 "${REPO_DIR}/.env"

# --- 3. Bot Python venv + deps -----------------------------------------------
echo "==> Creating bot virtualenv and installing requirements..."
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip --quiet
"${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/telegram_bot/requirements.txt" --quiet

# --- 4. Pipeline Python venv + deps ------------------------------------------
echo "==> Creating pipeline virtualenv and installing requirements..."
python3 -m venv "${REPO_DIR}/.venv-pipeline"
"${REPO_DIR}/.venv-pipeline/bin/pip" install --upgrade pip --quiet
"${REPO_DIR}/.venv-pipeline/bin/pip" install -r "${REPO_DIR}/pipeline-requirements.txt" --quiet

# Install dbt packages (dbt_utils)
echo "==> Installing dbt packages..."
cd "${REPO_DIR}/dbt"
"${REPO_DIR}/.venv-pipeline/bin/dbt" deps --profiles-dir . --target local 2>/dev/null || true
cd "${REPO_DIR}"

# --- 5. Install systemd USER services ----------------------------------------
echo "==> Installing systemd user services..."
mkdir -p "${HOME}/.config/systemd/user"

# 5a. Telegram bot service (long-running)
cat > "${HOME}/.config/systemd/user/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Polish IT Job Market - Telegram bot (long-polling)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${REPO_DIR}/.env
Environment=PYTHONUNBUFFERED=1
Environment=MPLBACKEND=Agg
Environment=PIPELINE_DB_PATH=${REPO_DIR}/pipeline.duckdb
ExecStart=${REPO_DIR}/.venv/bin/python -m telegram_bot.bot
Restart=on-failure
RestartSec=5
TimeoutStopSec=100

[Install]
WantedBy=default.target
EOF

# 5b. Pipeline service (oneshot, triggered by timer or SSH)
cat > "${HOME}/.config/systemd/user/${PIPELINE_SERVICE}.service" <<EOF
[Unit]
Description=Polish IT Job Market - Data pipeline (bronze → gold)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${REPO_DIR}/.env
Environment=PYTHONUNBUFFERED=1
Environment=PIPELINE_DB_PATH=${REPO_DIR}/pipeline.duckdb
ExecStart=${REPO_DIR}/.venv-pipeline/bin/python -m pipeline.run_pipeline
TimeoutStartSec=300
StandardOutput=journal
StandardError=journal
EOF

# 5c. Pipeline timer (daily fallback)
cat > "${HOME}/.config/systemd/user/${PIPELINE_SERVICE}.timer" <<EOF
[Unit]
Description=Daily pipeline timer (fallback if GitHub Actions trigger fails)

[Timer]
OnCalendar=*-*-* 04:00:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

# --- 6. Enable linger so it survives logout/reboot ---------------------------
echo "==> Enabling linger for ${RUN_USER} (survives logout + reboot)..."
sudo loginctl enable-linger "${RUN_USER}"

# --- 7. (Re)start services ---------------------------------------------------
echo "==> (Re)starting services..."
systemctl --user daemon-reload

# Bot
systemctl --user enable "${SERVICE_NAME}"
systemctl --user restart "${SERVICE_NAME}"

# Pipeline timer
systemctl --user enable "${PIPELINE_SERVICE}.timer"
systemctl --user restart "${PIPELINE_SERVICE}.timer"

sleep 3
echo
echo "==> Bot status:"
systemctl --user --no-pager status "${SERVICE_NAME}" || true
echo
echo "==> Pipeline timer status:"
systemctl --user --no-pager status "${PIPELINE_SERVICE}.timer" || true
echo
echo "Done. Follow bot logs:"
echo "    journalctl --user -u ${SERVICE_NAME} -f"
echo
echo "Run pipeline manually:"
echo "    systemctl --user start ${PIPELINE_SERVICE}"
echo "    journalctl --user -u ${PIPELINE_SERVICE} --no-pager"
echo
echo "IMPORTANT: stop the OLD bot on your laptop so two instances don't fight"
echo "over the same token (Telegram 'Conflict: terminated by other getUpdates')."
