#!/usr/bin/env bash
# Apply every schema file, in order, to a database that does not have them yet.
#
# For a managed instance (Cloud SQL) where nothing mounts an init directory —
# see docs/deploy-gcp.md §3. Compose does this automatically for local volumes.
#
# Usage:
#   scripts/bootstrap-db.sh <dsn>
#   scripts/bootstrap-db.sh postgresql://postgres:PW@localhost:6543/game_guide_ai
#
# Exit: 0 all files applied · 1 a file failed (nothing after it was attempted)
#       · 2 usage/environment error
#
# Order matters: 01 creates the vector extension and dnd schema, 02-03 add the
# corpus tables and hybrid search, 04 the chat schema, 05 the auth schema — whose
# ownership foreign key is added onto 04's table.
#
# Stopping at the first failure is the point. psql continues past a SQL error
# without ON_ERROR_STOP, and continuing past a failed FILE would run 05 against a
# database where 04 never created the table it references. A partially
# bootstrapped database is not a working one: minting the first invite with
# `python -m service.admin_invites` needs auth.users and auth.invites to exist.
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Corpus schema, then the canonical application schema (service/sql/ is the one
# definition the running service also applies — see service/schema.py).
readonly FILES=(
  "vector-db/init/01-extensions.sql"
  "vector-db/init/02-schema.sql"
  "vector-db/init/03-hybrid-search.sql"
  "service/sql/04-chat-schema.sql"
  "service/sql/05-auth-schema.sql"
)

usage() { echo "usage: $(basename "$0") <dsn>" >&2; exit 2; }

main() {
  [ $# -eq 1 ] || usage
  local dsn="$1"
  command -v psql >/dev/null 2>&1 || {
    echo "psql not found on PATH" >&2; exit 2
  }

  local f path
  for f in "${FILES[@]}"; do
    path="$REPO_ROOT/$f"
    [ -f "$path" ] || { echo "missing schema file: $f" >&2; exit 2; }
    echo "==> $f"
    if ! psql "$dsn" -v ON_ERROR_STOP=1 -f "$path"; then
      echo "BOOTSTRAP FAILED at $f — fix the error above, then re-run" >&2
      exit 1
    fi
  done
  echo "schema bootstrap complete (${#FILES[@]} files)"
}

main "$@"
