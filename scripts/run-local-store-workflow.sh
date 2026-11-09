#!/usr/bin/env bash
# Run the normal store-creation API against local SQLite. Scaffold requests
# launch a loopback Vite storefront instead of touching GitHub or providers.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
api_port="${1:-8001}"
if [[ ! "$api_port" =~ ^[0-9]+$ ]] || (( api_port < 1024 || api_port > 65535 )); then
  echo "Usage: $0 [local API port, default 8001]" >&2
  exit 1
fi

if [[ ! -f cert.pem || ! -f key.pem ]]; then
  echo "Local HTTPS cert.pem/key.pem are required for the existing secure auth cookies." >&2
  exit 1
fi

export DATABASE_URL="sqlite:///$repo_root/local.sqlite3"
export IS_DEBUG=True
export STOREFRONT_DEPLOYMENT_MODE=local
export STOREFRONT_RELEASE_REVISION=
export DEV_API_URL="https://127.0.0.1:$api_port"
export STOREFRONT_API_URL="https://localhost:5178"
export ALLOWED_HOSTS=127.0.0.1,localhost
export EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
export DJANGO_FILE_STORAGE=django.core.files.storage.FileSystemStorage
export DJANGO_MEDIA_ROOT="$repo_root/media"

venv/bin/python manage.py migrate --noinput
echo "Local store API: $DEV_API_URL"
echo "New local storefronts appear on http://127.0.0.1:5190-5199 after creation."
exec venv/bin/python manage.py runserver_plus --cert-file cert.pem --key-file key.pem "127.0.0.1:$api_port"
