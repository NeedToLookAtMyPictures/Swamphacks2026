#!/usr/bin/env bash
# Run the sync server and give it a public internet URL anyone can reach.
#
#   ./serve_public.sh                         # https://heuslerene.com/server (named tunnel "swamphacks")
#   CLOUDFLARE_TUNNEL= ./serve_public.sh      # cloudflared: no account, new random URL each run
#   NGROK_DOMAIN=<name>.ngrok-free.app ./serve_public.sh   # ngrok: the same URL every run
#
# The named tunnel needs a one-time setup; see README.md.
#
# The database stays on this machine (data/server.db). Devices on the same wifi
# can still reach it directly too. There is no login: anyone with the URL can
# read and add records. Stop with Ctrl+C, which stops the server too.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
CLOUDFLARE_TUNNEL="${CLOUDFLARE_TUNNEL-swamphacks}"   # set it empty for a quick random URL
export BIND="0.0.0.0:${PORT}"

if [[ -n "${NGROK_DOMAIN:-}" ]]; then
  command -v ngrok >/dev/null || { echo "ngrok isn't installed (see README.md)"; exit 1; }
elif ! command -v cloudflared >/dev/null; then
  echo "Neither cloudflared nor ngrok is set up. Install one (see README.md), e.g.:"
  echo "  sudo pacman -S cloudflared        # Arch"
  exit 1
fi

if curl -s -o /dev/null "http://localhost:${PORT}/"; then
  echo "Something is already running on port ${PORT} (an older server?)."
  echo "Stop it first, e.g. pkill -f 'gunicorn server:app', then run this again."
  exit 1
fi

gunicorn server:app &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

# Wait for the server before opening the tunnel
for _ in $(seq 50); do
  kill -0 "$SERVER_PID" 2>/dev/null || break  # gunicorn gave up (port taken?)
  curl -sf "http://localhost:${PORT}/api/health" >/dev/null && break
  sleep 0.2
done
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo
  echo "The server didn't start. If it says 'Address already in use', another server"
  echo "is already running on port ${PORT}: stop it (pkill -f 'gunicorn server:app') and try again."
  exit 1
fi

if [[ -n "${NGROK_DOMAIN:-}" ]]; then
  echo
  echo "Public URL: https://${NGROK_DOMAIN}   (database page: https://${NGROK_DOMAIN}/)"
  echo
  ngrok http --url="https://${NGROK_DOMAIN}" "${PORT}" --log stdout --log-level warn
else
  # cloudflared reads ~/.cloudflared/config.yml on its own, and that may hold
  # another tunnel's rules (which answer 404 for this one). Give it our own config.
  mkdir -p data
  CONFIG="data/cloudflared.yml"
  if [[ -n "${CLOUDFLARE_TUNNEL}" ]]; then
    TUNNEL_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="${CLOUDFLARE_TUNNEL}" '$2 == n {print $1}')"
    CREDENTIALS="${HOME}/.cloudflared/${TUNNEL_ID}.json"
    if [[ -z "${TUNNEL_ID}" || ! -f "${CREDENTIALS}" ]]; then
      echo "No tunnel named ${CLOUDFLARE_TUNNEL} with credentials in ~/.cloudflared (see README.md)."
      exit 1
    fi
    printf 'tunnel: %s\ncredentials-file: %s\ningress:\n  - service: http://localhost:%s\n' \
      "${TUNNEL_ID}" "${CREDENTIALS}" "${PORT}" > "${CONFIG}"
    echo
    echo "Public URL: https://heuslerene.com/server   (database page: https://heuslerene.com/server/)"
    echo
    cloudflared tunnel --no-autoupdate --config "${CONFIG}" run
  else
    echo "no-autoupdate: true" > "${CONFIG}"
    echo
    echo "Public URL: look for https://<random-words>.trycloudflare.com below."
    echo "It changes every time this script starts."
    echo
    cloudflared tunnel --config "${CONFIG}" --url "http://localhost:${PORT}"
  fi
fi
