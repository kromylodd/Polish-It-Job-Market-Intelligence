#!/usr/bin/env bash
#
# One-shot deployment of the Telegram bot to an always-on Linux VM
# (tested target: Oracle Cloud Always Free, Ubuntu 22.04/24.04 ARM64).
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
  echo "Copy your .env (TELEGRAM_BOT_TOKEN, ANALYTICS_SALT, DATABRICKS_* ...) here first."
  echo "  scp .env  <vm-user>@<vm-ip>:${REPO_DIR}/.env"
  exit 1
fi
chmod 600 "${REPO_DIR}/.env"

# --- 3. Python venv + deps ---------------------------------------------------
echo "==> Creating virtualenv and installing requirements..."
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/telegram_bot/requirements.txt"

# --- 4. Install systemd USER service ----------------------------------------
# A user service + linger keeps the bot alive across logout/reboot without
# needing a separate system account. Linger is what makes it truly 24/7.
echo "==> Installing systemd user service..."
mkdir -p "${HOME}/.config/systemd/user"
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
ExecStart=${REPO_DIR}/.venv/bin/python -m telegram_bot.bot
Restart=on-failure
RestartSec=5
TimeoutStopSec=100

[Install]
WantedBy=default.target
EOF

# --- 5. Enable linger so it survives logout/reboot --------------------------
echo "==> Enabling linger for ${RUN_USER} (survives logout + reboot)..."
sudo loginctl enable-linger "${RUN_USER}"

# --- 6. (Re)start the service -------------------------------------------------
echo "==> (Re)starting service..."
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"
systemctl --user restart "${SERVICE_NAME}"

sleep 3
echo
echo "==> Status:"
systemctl --user --no-pager status "${SERVICE_NAME}" || true
echo
echo "Done. Follow logs with:"
echo "    journalctl --user -u ${SERVICE_NAME} -f"
echo
echo "IMPORTANT: stop the OLD bot on your laptop so two instances don't fight"
echo "over the same token (Telegram 'Conflict: terminated by other getUpdates'):"
echo "    systemctl --user disable --now ${SERVICE_NAME}   # run this ON YOUR LAPTOP"
