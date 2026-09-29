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
`auth`, `app`, `campaign`, `audit` and `metering` schemas. At startup — before anything is served — the service calls
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
| `0004_campaign_schema.sql` | `campaign` | campaigns and their authorisation row, participants, enrolment codes, device credentials, table sessions, table credentials, the per-generation join counter (`1kg.2.1`). `0009` retired the enrolment codes and device credentials, and `0016` the table link's digest and the join counter; a table credential is now a screen grant |
| `0005_audit_events.sql` | `audit` | the append-only ledger (`service/audit_log.py`) |
| `0006_conversation_metadata.sql` | `chat` | `campaign_id`, `title`, `updated_at`, `archived_at` on `chat.conversations` |
| `0007_conversation_started_mode.sql` | `chat` | `started_mode` on `chat.conversations`, and `conversations_owner_recent_idx`, the owner's index page (`1kg.2.4`) |
| `0008_document_schema.sql` | `campaign` | documents and their versions: the live `data`, the `write_revision` and per-field revisions, the one open working version, the folded `name_key` / `search_key`, the character-sheet link, and the four library indexes (`1kg.5.1`) |
| `0009_participant_accounts.sql` | `campaign` | a participant becomes an account's seat (`agent-forge-harness-fma`): `user_id` (`REFERENCES auth.users`, `ON DELETE NO ACTION`) and `accepted_at` on `participants`, the CHECK that an accepted seat has an account (safe because both columns are new), one live seat per account per campaign and the account's own index (both partial); and it **drops** `enrolment_codes` and `device_credentials`. A drop is a contraction (section 3), shipped here because no build that reads those tables has ever been deployed — master's production build has no campaign schema — so there is no rollback to a build that needs them |
| `0010_timeline_entries.sql` | `chat` | the conversation timeline's typed entries (`1kg.4.2`): one row per exchange `POST /chat` answered, written best-effort after the answer — a minted `entry_id` whose CHECK `service.timeline_store.entry_id_check_regex()` generates, `entry_kind` against the kinds of this migration, `schema_version`, `created_at`, `seq` (the tiebreak), the validated `payload`, and `user_message_id` / `assistant_message_id`, the `chat.messages` rows it carries (each carried by at most one entry; both cascade). Cascades with its conversation; no campaign column |
| `0011_usage_ledger.sql` | `metering` | the provider-attempt cost ledger and its price table (`yje.5.1.2`): `provider_attempts`, one append-only row per provider attempt keyed by `(operation_id, attempt_index)`, with `occurred_at`, the closed-shape codes, the four token counts, `billed_account_id` and `campaign_id` as plain values (no foreign key out of the schema: a cost row outlives the account and the campaign it names), and `price_revision_id`, the revision in force when the row was written; `price_revisions`, one immutable row per `(provider, alias, effective_from)` with three `NUMERIC(12,6)` rates, seeded with `gpt-4o-mini`; `provider_attempts_account_time_idx` and `price_revisions_lookup_idx`. No update or delete path (`docs/runbooks/usage-capture.md` section 7) |
| `0012_seat_confirmation_and_offers.sql` | `campaign` | the GM's confirmation of a seat and offers by address (`1kg.2.2`, D-12, SEC-50): `confirmed_at` on `participants` with the CHECK that only an accepted seat is confirmed — **safe because the column is new**: every existing row has it NULL, and the previous build never writes it — and `UNIQUE (id, campaign_id)`, redundant with the primary key and there for a composite foreign key; **nothing updates a participant's `id` or `campaign_id`, so RQ-3's `FOR NO KEY UPDATE` is unaffected**. `seat_offers`: a seat and an address (`address`, and `address_key` folded by the application), `(participant_id, campaign_id)` → `participants` and `(campaign_id, offered_by)` → `campaigns (id, owner_id)`, both `ON DELETE CASCADE`, the outcome and its moment kept in step by a CHECK, one open offer per seat (a partial unique index), and four plain indexes; no predicate reads the clock. `seat_blocks`: `(blocker_user_id, blocked_owner_id)`, each cascading from `auth.users` |
| `0013_campaign_summary.sql` | `campaign` | what a campaign card in the tavern reads (bead cfx): `concluded_at`, `tone` (NULL or 1 to 80 characters, `campaigns_tone_chk`) and `game_system` (NOT NULL, default `dnd5e`, one allowed value, `campaigns_game_system_chk`) on `campaigns`. Every other fact on a card is derived at read time (`service/campaign_summary_store.py`) and no index is added. Concluded is not archive: it narrows nothing and is not an authorisation fact |
| `0014_tool_invocations.sql` | `campaign` | the GM tool invocation aggregate (`1kg.4.1`, slice A): `tool_invocations`, one row per invocation keyed by `(owner_id, campaign_id, invocation_id)` — the client's idempotency key, scoped to the GM and the campaign — with its request (`tool_id` against the tools of this migration, the trimmed `brief`, `source_entry_id`), its status, current `attempt` (1 to 100) and that attempt's `attempt_deadline`, the cancel flag, the typed `result` / `error` kept in step with the status by two CHECKs, and `entry_id`, the timeline entry that carries it (unique; cascades); the conversation edge cascades, and `(campaign_id, owner_id)` → `campaigns (id, owner_id)` is `NO ACTION DEFERRABLE INITIALLY DEFERRED`, for 0006's reason. `tool_attempts`, **the admission record**: one row per attempt with its `operation_id` (the link to `metering.provider_attempts`), `started_at`, `deadline_at`, and how it ended — no token, price, alias or provider column. A partial index serves the per-GM in-flight count and a plain one the pilot day's count. No hash of any text (ED-26); no predicate reads the clock |
| `0015_media_assets.sql` | `campaign` | GM-side media (`1kg.8.1.1`, slice a of `1kg.8.1`): `assets`, one row per asset holding what was declared, what the server measured, a state (the wire's four plus the storage-only tombstone `deleted`) and two immutable, independently minted object keys (`object_key`, `tmp_key`; each UNIQUE, never updated, so no `UPDATE` takes `FOR UPDATE` on a key column), never bytes; the command id with its partial unique index; one index, `(campaign_id, state)`. `campaign_id` is **`ON DELETE NO ACTION`**, written out: bytes leave the bucket only through the outbox, so a cascade would remove rows and leave their bytes named by nothing; PostgreSQL therefore refuses to delete a campaign (directly, or through its owner's account cascade) while any asset row remains, until `service/asset_store.py`'s campaign primitive has removed the rows and enqueued their byte deletions in the same transaction (`1kg.2.6`, `zkc`). `media_usage`, the per-campaign quota counter, is a table of its own so that reservations never contend on the campaign row; it cascades with its campaign, an `AFTER INSERT` trigger on `campaigns` writes its row (0004's `authz_state` pattern) and the same file backfills a zero row for every campaign that already exists. Every CHECK is on a new table |
| `0016_table_session_access.sql` | `campaign`, `audit` | the live table session without a link or a join (`1kg.2.3`, threat model section 15): it **drops** `table_sessions.link_digest` (its partial unique index goes with it) and `session_join_counters`; adds `start_command_id` and `rotate_command_id` to `table_sessions`, each NULL or the contract's `CommandId` shape, with one **partial** unique index over `(campaign_id, start_command_id)` and **no** index on `rotate_command_id` (a non-partial unique index would make its column a key, and Rotate's UPDATE would then take `FOR UPDATE` and block every screen-grant insert, RQ-3); and replaces `audit.events`' actor-kind CHECK, `guest` out and `screen` in. The drops are a contraction (section 3), shipped here because no build that reads either has ever been deployed and the store in the same change reads neither; the new columns' CHECKs validate only NULLs and the previous build writes neither column (0009's argument); the actor-kind CHECK validates every existing row, and no code has ever written `guest` to `audit.events` |

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

**An absorbed enqueue holds its row until it commits.** `INSERT ... ON CONFLICT DO
NOTHING` locks nothing, so between the absorb and the enqueuer's commit another
instance could claim the job, run it and delete it, and the enqueuer's change was
never processed (W-1, proven against PostgreSQL 17 in CI before it was fixed). The
absorb now reads the row `FOR SHARE`, which a claim's `FOR UPDATE SKIP LOCKED`
skips until every absorber has committed. Share locks are compatible, so two
enqueuers that both absorb into a committed job never wait for each other. **What
an enqueue can wait for:** a claim's own transaction on that row — one short
statement under the queue's bounds below — and if the claim wins, the enqueue
inserts a row of its own. It can **also** wait for another open transaction that
has enqueued the same kind and key and not yet committed: the unique index makes
the insert wait for that transaction's outcome. If that transaction rolled back,
the enqueue inserts its own row. If it committed, the enqueue absorbs into its job,
unless a claim has taken the job in the meantime: a claimed job has left the index,
so the enqueue then inserts a row of its own, as it does when a claim wins. Both
outcomes with no claim in between are proven against PostgreSQL in
`tests/test_db_postgres.py`. Nothing in the queue bounds that wait. No
timeout is set in the caller's transaction: that would re-time the rest of the
caller's work. Many concurrent share holders make a multixact, and in principle
could keep a claim off that one row for as long as absorbers keep arriving; each
holds it only until its own short transaction commits.

**Enqueuing several keys in one transaction needs a fixed order.** Because an
enqueue can wait on another open transaction's uncommitted enqueue of the same
key, two transactions that each enqueue keys `K1` and `K2` — one in that order,
the other reversed — can each end up waiting on the other's insert.
PostgreSQL's own deadlock detector finds the cycle and aborts one of them with
`40P01`; that is not a bug in the outbox, and no queue bound covers it, since
nothing bounds an enqueue's wait (above). A deadlock victim here is retried
exactly as a lock-timeout victim is (RQ-3). A caller that enqueues more than
one dedupe key in a transaction must therefore pick one fixed order for its
keys and use it every time, the way RQ-3 orders every row lock — never an
order that depends on the request.

#### Who runs jobs (1kg.2.7)

Nothing runs by itself: Cloud Run allocates CPU only during a request. The three
drivers RT-15 names are wired in `service/job_driver.py`:

| Driver | When | Runs |
|--------|------|------|
| `JobRunner.run_after_commit` | when the enqueuing transaction commits — **inside** that request, before its response | the one job |
| `job_driver.run_after_response` | a Starlette background task, **after** the response has been sent | the one job |
| the request hook (`JobHookMiddleware`) | after a signed-in request has been answered; never `/healthz`, `/internal` or `/table`; at most once every 5 s per instance, backing off 30 s after an outage | one due job |
| `POST /internal/jobs` | Cloud Scheduler, every ten minutes (`docs/deploy-gcp.md` §12) | up to 20 due jobs, none started after 30 s |

Use `run_after_response` whenever the answer must not wait for the job — a
revocation's acknowledgement never waits on its reconciliation (RQ-5). The first
three are opportunistic, since Cloud Run may withhold the CPU once the final body
is sent; the durable row and the scheduler are what make "retried until done"
true. With no kind registered the hook never asks the database, so it adds no
query to a chat request.

**One attempt at a time per instance.** Every driver takes one lock without
waiting — before it asks the gate for a connection — and, if job work is already
in flight, does not make its attempt (`/internal/jobs` answers `ran: 0, remaining:
true` at once). Job work therefore holds **at most one** of the gate's
connections, on every path; nothing queues behind it, so nothing accumulates, and
login, chat and table traffic keep the rest (SEC-35). A gate with no free
connection (`PoolTimeout`) is "busy": the attempt is skipped. An `OperationalError`
or `OSError` is an outage: its class and SQLSTATE are logged, and the hook backs
off. A degraded instance (`migrations` not `current` or `ahead`) runs nothing, and
the scheduler route answers it with a content-free 503. Every driver runs the
synchronous runner in the thread pool, never on the event loop, and nothing that
goes wrong in job work reaches a response or a log beyond a kind, an id, an
exception class and a SQLSTATE.

**The bound is soft — this is exactly what it covers.** Every transaction the
queue opens (`claim`, `complete`, `fail`) begins with `set_config(..., true)` for
`lock_timeout` 2 s, `statement_timeout` 2 s and `transaction_timeout` 5 s,
transaction-scoped because `Database.connection()` is not in autocommit. Beside
them stands the driver's `connect_timeout`. These are **server-side defence in
depth for the database, not a client wall clock**: they do **not** cover `COMMIT`,
and they do nothing against a black-holed network, where the client waits on a
socket the server never answers. A handler is given a `JobContext` deadline that
is **advisory** — nothing interrupts a running handler, so it must bound its own
I/O — and `/internal/jobs`' budget is **cooperative**: it stops starting jobs and
never interrupts one. From outside, Cloud Run's request timeout and Cloud
Scheduler's attempt deadline bound the call. Every ordinary synchronous route in
this service has the same exposure; the job path is held to that standard, not a
higher one. A hard client-side deadline is `1kg.2.8`'s question. An attempt whose
commit outcome is unknown is already safe: `complete` is an idempotent delete,
`fail` is fenced by `attempts`, and a lease that is never finalised runs out.

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
