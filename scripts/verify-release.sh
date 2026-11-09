#!/usr/bin/env bash

set -euo pipefail

SERVER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESIGN_REPO_PATH="${DESIGN_STOCK_PATH:-${SERVER_ROOT}/../design-stock}"
PYTHON_BIN="${PLATFORM_PYTHON_BIN:-${SERVER_ROOT}/venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Backend Python environment is unavailable at ${PYTHON_BIN}." >&2
  exit 1
fi

if [[ ! -f "${DESIGN_REPO_PATH}/package.json" ]]; then
  echo "Design workspace is unavailable at ${DESIGN_REPO_PATH}." >&2
  exit 1
fi

cd "${SERVER_ROOT}"
TEST_MEDIA_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_MEDIA_ROOT}"' EXIT
TEST_ENV=(
  env
  DATABASE_URL=sqlite:///:memory:
  STOREFRONT_RELEASE_REVISION=
  DESIGN_STOCK_PATH="${DESIGN_REPO_PATH}"
  DJANGO_MEDIA_ROOT="${TEST_MEDIA_ROOT}"
)
"${TEST_ENV[@]}" "${PYTHON_BIN}" manage.py check
"${TEST_ENV[@]}" "${PYTHON_BIN}" manage.py makemigrations --check --dry-run
"${TEST_ENV[@]}" "${PYTHON_BIN}" manage.py test --noinput

npm --prefix "${DESIGN_REPO_PATH}" run release:verify
