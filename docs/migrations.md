# Migrations, transactions and pools

How the application schema changes, how a multi-step write stays atomic, and how
many database connections the service may hold. Introduced by `1kg.1.5`; shared by
every epic that adds a table (`1kg`, `1ir`, `yje`).

| Piece | Where |
|---|---|
| Ordered migrations and their manifest | `service/sql/migrations/` |
| The runner and its CLI | `service/migrations.py` |
| The connection gate and the realtime pool, the transaction boundary, the in-memory twin | `service/db.py` |
| The job outbox and its runner | `service/jobs.py` |
| Tests without a database | `service/tests/test_migrations.py`, `test_db.py`, `test_jobs.py`, `test_startup_migrations.py` |
| Tests against a real PostgreSQL (CI) | `tests/test_migrations_db.py`, `tests/test_db_postgres.py`, `tests/test_campaign_db.py`, `tests/test_schema.py` |

The **corpus** schema (`dnd`, `vector-db/init/`) is not part of this. It needs the
`vector` extension and an ingested corpus, belongs to the ingestion pipeline, and is
still applied by the container's init directory or `scripts/bootstrap-db.sh`.

## 1. How the schema is applied

`service/sql/migrations/NNNN_snake_case.sql` is the one definition of the `chat`,
`auth`, `app`, `campaign` and `audit` schemas. At startup — before anything is served — the service calls
`migrate()`, which applies whatever the database has not seen yet: once, in order,
each file in its own transaction together with its row in `app.schema_migrations`
(version, name, SHA-256 of the LF-normalised file, when, how long, which revision).

- **One path.** A fresh database and an old one are both brought up by the runner.
  Compose's init directory and `bootstrap-db.sh` no longer apply application DDL, so
  there is no second mechanism to drift from.
- **Concurrent startups** serialise on a session advisory lock; the loser re-reads
  the ledger under the lock and finds nothing to do. It waits at most 150 s.
- **A current database costs two small reads and takes no lock.** The DDL this replaced
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
| The database answers but refuses (no privilege to create or read the ledger) | `MigrationFailed` — **startup fails**; it would refuse again at every start |
| The packaged files do not match `manifest.txt`, have a gap or a duplicate number | `MigrationPackageError` — **startup fails** (and CI failed first) |
| A pool or mode setting is out of bounds, or a connection string does not parse | `ValueError` — **startup fails**; the string is never repeated |
| The image shipped without its migrations directory | `MigrationPackageError` — **startup fails** (as an `OSError` it would have read as an outage, and the broken revision would have taken the traffic) |
| A file ended the runner's transaction (`COMMIT;`, `END;` …) | `MigrationFailed` — **startup fails**, with no ledger row; what the file committed itself has to be repaired by hand |
| The database has migrations this build does not know | **Served.** `/healthz` says `migrations: "ahead"` — an older image mid-rollout or after a rollback |
| The database is unreachable | Three tries, two seconds apart; then **served, degraded**: no history, auth endpoints 503, `migrations: "unavailable"`. The log names the error class and SQLSTATE, never the driver's text. The instance **looks again by itself** — at most one short attempt every 15 s, made by one request at a time — checks or applies the schema exactly as at startup, and builds its stores when that succeeds |
| …and the database comes back with a schema this build refuses | The looking stops, the refusal is logged as an error, `migrations: "failed"`, and the instance stays degraded: a running instance cannot be kept out of traffic the way a starting one can, but it must not serve a schema it does not understand |

On Cloud Run a revision that fails to start never receives traffic, so a verdict
leaves the previous revision serving the schema it understands. `/healthz` gains
`migrations` (`current`, `ahead`, `unavailable`, `failed`, or `unchecked` for a
process that never ran the startup path); `status` and `ready` are unchanged.

### The operator's commands

```bash
python -m service.migrations status     # applied / pending / applied by a newer build; exit 1 if pending
python -m service.migrations migrate    # apply what is pending
python -m service.migrations verify     # packaged files against the manifest; needs no database
python -m service.migrations manifest   # append new files to the manifest
```

They read `MIGRATIONS_DATABASE_URL`, then `DATABASE_URL`. They never print a DSN.

**Only two things change a schema: the service's startup and an explicit
`migrate`.** `python -m service.admin_invites` (and the stores' `ensure_schema()`)
only *check*: run from an operator's checkout, which is not the deployed image, they
would otherwise apply whatever migrations that checkout happens to hold. With
something pending they stop and name the command to run.

### Adopting an existing database

Every database that predates the ledger — production included — already holds the
`chat` and `auth` objects. The first run finds no `app.schema_migrations`, creates
it, and applies 0001 and 0002 *over* what is there: both are idempotent and guard
every constraint change, so existing rows are untouched and nothing is rebuilt. From
then on the database is indistinguishable from a fresh one, which
`tests/test_migrations_db.py` proves by comparing the two column for column.

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
- **An *unreleased* one may still be edited in place**, and only while it is
  unreleased. **Unreleased means the file exists on no branch but its own pull
  request's.** From the moment it is merged into `master` or into an
  `integration/**` branch it is *released* — whether or not anything has been
  deployed — and it is never edited in place again. Deployment is not the line,
  because a shared branch is: other worktrees, other pull requests stacking
  migrations on top, and any developer's Compose stack (which keeps a persistent
  volume and applies migrations at start) can all have applied it by then, and
  each of those databases would meet the changed checksum as drift. While it is
  still only on its own pull request the file has been applied nowhere but CI's
  throwaway databases, so no ledger row anywhere contradicts its checksum, and a
  seventh file to correct a sixth that nothing has ever run would be history
  nobody needs. Editing one is the same mechanical act as the rule above —
  re-pin the line by hand, in the diff — so the pull request must **say which
  lines it re-pinned and why**, and a reviewer must confirm the file is really
  unreleased.
- **One number, one file.** Two branches that each add a migration both append to
  `manifest.txt`, so git reports a conflict instead of merging two `0007`s. Renumber
  the later one before merging.
- **No transaction control** (`BEGIN;`, `COMMIT;`, `END;`). The runner owns the
  transaction; a `COMMIT` half way would leave a change the ledger never recorded.
  A lint catches the common spellings in CI, and the runner asks the server after
  every file whether it is still inside its transaction.
- **Not supported yet:** statements that cannot run in a transaction (`CREATE INDEX
  CONCURRENTLY`). Tables are pilot-sized; add a no-transaction mode when one is not.
- **Only 0001 and 0002 are idempotent**, because every database that predates the
  ledger already holds their objects and the runner applies them over it once
  ("adoption"). Later migrations run exactly once and need no `IF NOT EXISTS`.
- **Rows that are not user content stay that way**: the outbox, the ledger and any
  audit table hold ids and codes, never text a user wrote (SEC-20).

### The files, and what each one owns

| File | Schema | What it adds |
|---|---|---|
| `0001_chat_schema.sql` | `chat` | conversations, messages, attachments |
| `0002_auth_schema.sql` | `auth` | users, invites, and the ownership foreign keys |
| `0003_jobs_outbox.sql` | `app` | the job outbox (`service/jobs.py`) |
| `0004_campaign_schema.sql` | `campaign` | campaigns and their authorisation row, participants, enrolment codes, device credentials, table sessions, table credentials, the per-generation join counter (`1kg.2.1`) |
| `0005_audit_events.sql` | `audit` | the append-only ledger (`service/audit_log.py`) |
| `0006_conversation_metadata.sql` | `chat` | `campaign_id`, `title`, `updated_at`, `archived_at` on `chat.conversations` |
| `0007_document_schema.sql` | `campaign` | documents and their versions: the live `data`, the `write_revision` and per-field revisions, the one open working version, the folded `name_key` / `search_key`, the character-sheet link, and the four library indexes (`1kg.5.1`) |

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

### Ownership belongs in the query

Every aggregate table carries the key it is owned through (`campaign_id`, and
through the campaign its owner), so that a read or a write can name the caller in
the same statement that names the row — `... WHERE id = %s AND campaign_id IN
(SELECT id FROM campaigns WHERE owner_id = %s)` — and a row that is not the caller's
is indistinguishable from one that does not exist (SEC-2: one query, one 404).
Fetch-then-check is two chances to forget the check; a schema that cannot express
the single query is a schema bug, caught in the review of its migration.

### The outbox carries jobs only

`app.jobs` (migration 0003) holds work that must happen because a transaction
committed — deletions and sweeps. It is not an event log; realtime delivery reads
current state (RT-5). A claim is a lease taken with `FOR UPDATE SKIP LOCKED`, so a
handler holds no connection while it calls another service, an instance that dies
lets its lease expire, and **handlers must be idempotent**. Only kinds the running
build has a handler for are claimed, so an older instance leaves a newer build's jobs
alone. A `dedupe_key` absorbs a second request only into a job **nobody has claimed
yet**: a running handler may already have read the state it acts on, so later work
gets a row of its own. A payload is a flat object of identifiers; a failure stores
the exception's class name, never its message.

Nothing runs by itself: Cloud Run allocates CPU only during a request. RT-15 names
three callers for `JobRunner` — after the commit (`run_after_commit`, available now),
a hook on ordinary requests, and an authenticated `/internal/jobs` for Cloud
Scheduler. The last two arrive with the first job kinds (`1kg.8.1`, `1kg.9.5`);
adding them with no job to run would only add a query to every chat request.

## 5. Connections

`db-f1-micro` accepts 22 application connections, and Cloud Run gives an instance CPU
only while a request is in flight. The second fact decides the design:

- **Routes go through a gate, not a keep-alive pool.** At most `DB_POOL_MAX`
  connections are open at once per instance, each opened for one operation and closed
  after it — what the stores always did, now bounded. A keep-alive pool cannot shed
  idle connections from an instance that has been frozen, so every quiet instance,
  and during a rollout every instance of the *previous revision*, would keep holding
  its share of the 22. With the gate an idle instance holds nothing, no connection is
  ever stale, and the only cost is the connect the service was already paying.
- **The realtime path gets a small keep-alive pool.** A stream is a request, so its
  instance has CPU while it matters. The pool opens on first use, checks a connection
  as it lends it, sheds one idle for 60 s, and marks its sessions
  `idle_session_timeout = 120s` so the *server* reaps what a frozen instance left.
- **The budget**, checked by `tests/test_deploy_contract.py` against
  `scripts/deploy.sh`: steady state is (4 gate + 3 realtime + 1 listener) × 2
  instances + 2 for the operator = 18; a rollout overlaps two revisions, so the gate
  alone is 4 × 4 + 1 migration session + 2 = 19. The realtime pool and the listener
  are not in use yet; the beads that turn them on must redo the overlap sum
  (`1kg.7.5`, `1kg.9.5`). Raise a bound only together with the database tier.

| Variable | Default | Bounds | Meaning |
|---|---|---|---|
| `DB_POOL_MAX` | 4 | 0–10 | The gate: connections routes may have open at once. `0` removes it (unbounded, as before) |
| `DB_ASYNC_POOL_MAX` | 3 | 0–5 | The realtime pool |
| `DB_POOL_TIMEOUT_S` | 5 | 1–60 | How long a request waits for its turn before it fails as 503 |
| `CAMPAIGN_LOCK_TIMEOUT_S` | half the gate, at most 2 | 0.05–4 | How long a request waits for a campaign's authorisation row. **Must be below `DB_POOL_TIMEOUT_S`**: a request waiting for the lock is holding one of the gate's connections. Unset, it is derived from the gate, so every documented gate starts; set, a value that is not below the gate is refused by name at startup |
| `CAMPAIGN_TRANSACTION_TIMEOUT_S` | 5 | 1–60 | How long a transaction holding that row may live (RQ-8). A single long caller passes its own bound instead of raising this for everyone |
| `MIGRATIONS_DATABASE_URL` | — | | The schema owner's DSN, when it differs from the runtime's |
| `MIGRATIONS_MODE` | `apply` | `apply`, `verify` | `verify` never applies; pending migrations then stop startup |

The message and auth stores and retrieval all go through the gate — also on an
instance that started during an outage, so retrieval never leaves the budget. Sessions
are labelled `game-guide-ai:sync`, `:async` or `:migrate` in `pg_stat_activity`.
Shutdown closes the realtime pool and refuses new operations.

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
