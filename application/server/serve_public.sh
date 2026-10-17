#!/usr/bin/env bash
# Run the sync server and give it a public internet URL anyone can reach.
#
#   ./serve_public.sh                         # cloudflared: no account, new random URL each run
#   NGROK_DOMAIN=<name>.ngrok-free.app ./serve_public.sh   # ngrok: the same URL every run
#
# The database stays on this machine (data/server.db). Devices on the same wifi
# can still reach it directly too. There is no login: anyone with the URL can
# read and add records. Stop with Ctrl+C, which stops the server too.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
export BIND="0.0.0.0:${PORT}"

if [[ -n "${NGROK_DOMAIN:-}" ]]; then
  command -v ngrok >/dev/null || { echo "ngrok isn't installed (see README.md)"; exit 1; }
elif ! command -v cloudflared >/dev/null; then
  echo "Neither cloudflared nor ngrok is set up. Install one (see README.md), e.g.:"
  echo "  sudo pacman -S cloudflared        # Arch"
  exit 1
fi

gunicorn server:app &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

# Wait for the server before opening the tunnel
for _ in $(seq 50); do
  curl -sf "http://localhost:${PORT}/api/health" >/dev/null && break
  sleep 0.2
done

if [[ -n "${NGROK_DOMAIN:-}" ]]; then
  echo
  echo "Public URL: https://${NGROK_DOMAIN}   (database page: https://${NGROK_DOMAIN}/)"
  echo
  ngrok http --url="https://${NGROK_DOMAIN}" "${PORT}" --log stdout --log-level warn
else
  echo
  echo "Public URL: look for https://<random-words>.trycloudflare.com below."
  echo "It changes every time this script starts."
  echo
  cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}"
fi
