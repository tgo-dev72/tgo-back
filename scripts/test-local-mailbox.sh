#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipient="${1:-atelier@mail.tijarago.com}"
api_port="${LOCAL_MAIL_API_PORT:-8002}"
worker_port="${LOCAL_MAIL_WORKER_PORT:-8787}"
secret="${MAILBOX_INBOUND_SECRET:-tijarago-local-mailbox-secret}"
message_id="<local-mail-$(date +%s)-$$@tijarago.test>"

node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
if (( node_major < 22 )); then
  echo "Node.js 22 or newer is required by Wrangler (current: $(node --version)). Run 'nvm use 24' first." >&2
  exit 1
fi
api_log="$(mktemp)"
worker_log="$(mktemp)"

cleanup() {
  [[ -n "${worker_pid:-}" ]] && kill "$worker_pid" 2>/dev/null || true
  [[ -n "${api_pid:-}" ]] && kill "$api_pid" 2>/dev/null || true
  rm -f "$api_log" "$worker_log"
}
trap cleanup EXIT

cd "$repo_root"
DATABASE_URL="sqlite:///$repo_root/local.sqlite3" \
IS_DEBUG=True \
ALLOWED_HOSTS=127.0.0.1,localhost \
MAILBOX_TRANSPORT_MODE=local \
MAILBOX_INBOUND_SECRET="$secret" \
venv/bin/python manage.py runserver --noreload "127.0.0.1:$api_port" >"$api_log" 2>&1 &
api_pid=$!

for _ in {1..40}; do
  curl --silent --output /dev/null "http://127.0.0.1:$api_port/" 2>/dev/null && break
  sleep 0.25
done
kill -0 "$api_pid" 2>/dev/null || { cat "$api_log" >&2; exit 1; }

cd "$repo_root/workers/store-mailbox"
npx wrangler dev --port "$worker_port" \
  --var "API_URL:http://127.0.0.1:$api_port" \
  --var "INBOUND_SECRET:$secret" >"$worker_log" 2>&1 &
worker_pid=$!

for _ in {1..80}; do
  curl --silent "http://127.0.0.1:$worker_port/" >/dev/null 2>&1 && break
  sleep 0.25
done
kill -0 "$worker_pid" 2>/dev/null || { cat "$worker_log" >&2; exit 1; }

curl --silent --show-error --fail-with-body --request POST \
  "http://127.0.0.1:$worker_port/cdn-cgi/local/email?from=customer@example.com&to=$recipient" \
  --data-binary @- <<EOF
From: "Local Customer" <customer@example.com>
To: $recipient
Subject: Local mailbox end-to-end test
Date: $(date -R)
Message-ID: $message_id
Content-Type: text/plain; charset=utf-8

This message passed through the local Cloudflare Worker and Django ingestion endpoint.
EOF

echo
echo "Injected $message_id into $recipient"
echo "Refresh Dashboard -> Mailbox to see the conversation."
