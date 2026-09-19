# Migrations, transactions and pools

How the application schema changes, how a multi-step write stays atomic, and how
many database connections the service may hold. Introduced by `1kg.1.5`; shared by
every epic that adds a table (`1kg`, `1ir`, `yje`).

| Piece | Where |
|---|---|
| Ordered migrations and their manifest | `service/sql/migrations/` |
| The runner and its CLI | `service/migrations.py` |
| Pools, the transaction boundary, the in-memory twin | `service/db.py` |
| The job outbox and its runner | `service/jobs.py` |
| Tests without a database | `service/tests/test_migrations.py`, `test_db.py`, `test_jobs.py`, `test_startup_migrations.py` |
| Tests against a real PostgreSQL (CI) | `tests/test_migrations_db.py`, `tests/test_db_postgres.py`, `tests/test_schema.py` |

The **corpus** schema (`dnd`, `vector-db/init/`) is not part of this. It needs the
`vector` extension and an ingested corpus, belongs to the ingestion pipeline, and is
still applied by the container's init directory or `scripts/bootstrap-db.sh`.

## 1. How the schema is applied

`service/sql/migrations/NNNN_snake_case.sql` is the one definition of the `chat`,
`auth` and `app` schemas. At startup — before anything is served — the service calls
`migrate()`, which applies whatever the database has not seen yet: once, in order,
each file in its own transaction together with its row in `app.schema_migrations`
(version, name, SHA-256 of the LF-normalised file, when, how long, which revision).

- **One path.** A fresh database and an old one are both brought up by the runner.
  Compose's init directory and `bootstrap-db.sh` no longer apply application DDL, so
  there is no second mechanism to drift from.
- **Concurrent startups** serialise on a session advisory lock; the loser re-reads
  the ledger under the lock and finds nothing to do. It waits at most 150 s.
- **A current database costs one `SELECT` and takes no lock.** The DDL this replaced
  was re-run at every cold start and took `ACCESS EXCLUSIVE` locks on live tables
  each time.
- **Each migration runs under `lock_timeout = 10s` and `statement_timeout = 120s`**,
  so one that cannot get its table fails instead of queueing in front of all traffic.

### What stops startup, and what does not

| Situation | Result |
|---|---|
| A released file's bytes changed; a file was renamed; a migration sits below one already applied | `MigrationDriftError` — **startup fails** |
| A migration's SQL fails | Rolled back whole; `MigrationFailed` — **startup fails**. The message carries the SQLSTATE and primary message, never the `DETAIL` line (it quotes row values) |
| Another instance held the lock for 150 s | `MigrationLockTimeout` — **startup fails** |
| The packaged files do not match `manifest.txt`, have a gap or a duplicate number | `MigrationPackageError` — **startup fails** (and CI failed first) |
| A pool or mode setting is out of bounds | `ValueError` — **startup fails** |
| The database has migrations this build does not know | **Served.** `/healthz` says `migrations: "ahead"` — an older image mid-rollout or after a rollback |
| The database is unreachable | **Served, degraded, as before**: no history, auth endpoints 503, `migrations: "unavailable"` |

On Cloud Run a revision that fails to start never receives traffic, so a verdict
leaves the previous revision serving the schema it understands. `/healthz` gains
`migrations` (`current`, `ahead`, `unavailable`, or `unchecked` for a process that
never ran the startup path); `status` and `ready` are unchanged.

### The operator's commands

```bash
python -m service.migrations status     # applied / pending / applied by a newer build; exit 1 if pending
python -m service.migrations migrate    # apply what is pending
python -m service.migrations verify     # packaged files against the manifest; needs no database
python -m service.migrations manifest   # append new files to the manifest
```

They read `MIGRATIONS_DATABASE_URL`, then `DATABASE_URL`. They never print a DSN.

## 2. Adding a migration

1. Create `service/sql/migrations/NNNN_what_it_does.sql` with the next number.
2. `python -m service.migrations manifest` — appends its line to `manifest.txt`.
3. Run the suites; `tests/test_migrations_db.py` applies every file to a fresh
   database and to a pre-expansion one in CI.

Rules the runner or CI enforce:

- **A released migration is never edited.** `manifest.txt` pins every checksum and
  the tool only ever appends, so an edit fails `discover()` in CI; changing a pinned
  line has to be done by hand, in a diff a reviewer sees. The only legitimate case is
  a file that *could not* apply anywhere — say so in the pull request.
- **One number, one file.** Two branches that each add a migration both append to
  `manifest.txt`, so git reports a conflict instead of merging two `0007`s. Renumber
  the later one before merging.
- **No transaction control** (`BEGIN;`, `COMMIT;`). The runner owns the transaction;
  a `COMMIT` half way would leave a change the ledger never recorded.
- **Not supported yet:** statements that cannot run in a transaction (`CREATE INDEX
  CONCURRENTLY`). Tables are pilot-sized; add a no-transaction mode when one is not.
- **Only 0001 and 0002 are idempotent**, because every database that predates the
  ledger already holds their objects and the runner applies them over it once
  ("adoption"). Later migrations run exactly once and need no `IF NOT EXISTS`.
- **Rows that are not user content stay that way**: the outbox, the ledger and any
  audit table hold ids and codes, never text a user wrote (SEC-20).

## 3. Roll forward, never back

There are no down migrations. A schema mistake is fixed by the next migration.

- **Every migration must work with the previous release's code.** During a rollout
  both builds run against the same database, and rolling back the *image* — sending
  Cloud Run traffic to the previous revision — has to stay possible without touching
  the database. That older build reports `ahead` and keeps serving.
- **Expand, then contract, across releases.** Adding a table, a nullable column or an
  index is an expansion and is always safe. Renaming or dropping something, adding
  `NOT NULL`, or changing a type is a contraction: ship the expansion and the code
  that uses it first; ship the contraction in a later release, once no rollback to a
  build that needs the old shape is intended.
- **Backfills are separate from shape changes** and must fit the statement timeout;
  a large one is a job (`service/jobs.py`), not a migration.
- **A failed migration leaves nothing behind.** The new revision does not start, the
  old one keeps serving, and the deploy can simply be retried if the cause was
  transient (a lock timeout).
- **Restoring the database is the last resort** (Cloud SQL backups,
  `docs/deploy-gcp.md`): it loses writes. The ledger is restored with the data, so
  the next startup re-applies whatever the restore took away.

## 4. Transactions and repositories

```python
with db.transaction() as unit:            # Database or InMemoryDatabase
    documents.archive(unit, document_id)   # the aggregate
    jobs.enqueue(unit, "asset.delete", {"asset_id": asset_id})   # its outbox job
    unit.notify("table_session", session_id)                     # a wake-up, on commit
```

Everything written through one unit of work commits or rolls back together; that is
how "stop and archive", "stop and unlink" and the atomic reveal mutation are to be
built. `unit.on_commit(fn)` runs only after the commit, after the connection is back
in the pool. `unit.lock(AdvisoryLock.X, key)` serialises transactions on one key
(RT-8's seat check). A notification is a wake-up, never the record: its payload is
an id of at most 200 characters, and readers fetch state.

A store keeps the repository's pattern: a `Protocol`, an in-memory implementation
for unit tests, a Postgres one. `InMemoryDatabase` gives the fakes the same boundary
— a fake changes its state at once and registers `unit.on_rollback(undo)`; a failed
block undoes in reverse and releases no notification and no callback. Keep
transactions short, and make no network call inside one.

### The outbox carries jobs only

`app.jobs` (migration 0003) holds work that must happen because a transaction
committed — deletions and sweeps. It is not an event log; realtime delivery reads
current state (RT-5). A claim is a lease taken with `FOR UPDATE SKIP LOCKED`, so a
handler holds no connection while it calls another service, an instance that dies
lets its lease expire, and **handlers must be idempotent**. Only kinds the running
build has a handler for are claimed, so an older instance leaves a newer build's jobs
alone. A payload is a flat object of identifiers; a failure stores the exception's
class name, never its message.

Nothing runs by itself: Cloud Run allocates CPU only during a request. RT-15 names
three callers for `JobRunner` — after the commit (`run_after_commit`, available now),
a hook on ordinary requests, and an authenticated `/internal/jobs` for Cloud
Scheduler. The last two arrive with the first job kinds (`1kg.8.1`, `1kg.9.5`);
adding them with no job to run would only add a query to every chat request.

## 5. Connections

`db-f1-micro` accepts 22 application connections. Per instance the service may hold
6 (synchronous pool, routes) + 3 (asynchronous pool, realtime; opened on first use) +
1 (the realtime listener, outside the pools) = 10; two instances is 20, leaving 2 for
the operator. `tests/test_deploy_contract.py` checks that arithmetic against
`scripts/deploy.sh`.

| Variable | Default | Bounds | Meaning |
|---|---|---|---|
| `DB_POOL_MAX` | 6 | 0–10 | Synchronous pool. `0` turns pooling off: a connection per operation, as before |
| `DB_ASYNC_POOL_MAX` | 3 | 0–5 | Asynchronous pool |
| `DB_POOL_TIMEOUT_S` | 5 | 1–60 | How long a request waits for a connection before it fails as 503 |
| `MIGRATIONS_DATABASE_URL` | — | | The schema owner's DSN, when it differs from the runtime's |
| `MIGRATIONS_MODE` | `apply` | `apply`, `verify` | `verify` never applies; pending migrations then stop startup |

Both pools keep **no idle minimum**: an instance that serves nothing holds nothing.
Every connection is checked as it is lent, and a pool nobody borrowed from for 30 s is
swept first — Cloud Run freezes an instance between requests and its sockets die
quietly. The message and auth stores and retrieval all borrow from the synchronous
pool. Sessions are labelled `game-guide-ai:sync`, `:async`, `:direct` or `:migrate`
in `pg_stat_activity`. Shutdown closes both pools.

`MIGRATIONS_DATABASE_URL` and `MIGRATIONS_MODE=verify` are the seam for
least-privilege roles: a runtime role without DDL rights, and a deploy step that runs
`migrate` as the owner. The roles themselves are `1ir.1.13`'s decision; until then
the service owns its schema, as it always has.

## 6. Running the database-backed tests

```bash
docker compose up -d vector-db
DATABASE_URL=postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai \
  uv run python -m pytest -q --no-cov tests/test_migrations_db.py tests/test_db_postgres.py tests/test_schema.py
```

Each test creates and drops a database of its own. Without `DATABASE_URL` they skip;
CI always sets it, and `service/tests/test_ci_workflow.py` guards that wiring.
