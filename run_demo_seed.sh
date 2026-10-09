#!/usr/bin/env bash
#
# run_demo_seed.sh — seed the Smarties Foods MTO demo company.
#
# This is a thin, safe wrapper around seed_smarties_foods.py. It only SEEDS
# data; it does not start any servers.
#
# WHICH DATABASE?
#   The seeder writes to whatever the app's DATABASE_URL points at.
#     • No DATABASE_URL set  -> local db.sqlite3   (safe, isolated)
#     • DATABASE_URL set     -> that Postgres       (e.g. the deployed Railway DB)
#
# USAGE
#   Local SQLite demo:
#       ./run_demo_seed.sh --local
#
#   Deployed DB (reads DATABASE_URL from .env, or export it yourself):
#       ./run_demo_seed.sh --deployed
#
#   Default (no flag) uses whatever DATABASE_URL is already in the environment.
#
set -euo pipefail

cd "$(dirname "$0")"

# Activate the project virtualenv if present.
if [ -f ".venv/Scripts/activate" ]; then
  # Windows (Git Bash)
  # shellcheck disable=SC1091
  source .venv/Scripts/activate
elif [ -f ".venv/bin/activate" ]; then
  # Linux / macOS
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

TARGET="${1:-}"

case "$TARGET" in
  --local)
    unset DATABASE_URL || true
    echo ">> Target: LOCAL SQLite (db.sqlite3)"
    ;;
  --deployed)
    # Load DATABASE_URL from .env if not already exported.
    if [ -z "${DATABASE_URL:-}" ] && [ -f ".env" ]; then
      export "$(grep -E '^DATABASE_URL=' .env | head -1)"
    fi
    if [ -z "${DATABASE_URL:-}" ]; then
      echo "!! --deployed given but no DATABASE_URL found (env or .env). Aborting." >&2
      exit 1
    fi
    DB_HOST="$(printf '%s' "$DATABASE_URL" | sed -E 's|.*@([^:/]+).*|\1|')"
    echo ">> Target: DEPLOYED Postgres (host: ${DB_HOST})"
    echo ">> This writes a full demo company into that database."
    read -r -p ">> Type 'yes' to continue: " CONFIRM
    [ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 1; }
    ;;
  "")
    echo ">> Target: current environment DATABASE_URL (sqlite if unset)"
    ;;
  *)
    echo "Unknown option: $TARGET (use --local or --deployed)" >&2
    exit 1
    ;;
esac

echo ">> Applying migrations..."
python manage.py migrate --no-input

echo ">> Seeding Smarties Foods demo..."
python seed_smarties_foods.py

echo ">> Done. Log in with the credentials printed above (password: SmartiesDemo@2026)."
