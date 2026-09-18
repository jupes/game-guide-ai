# GM Workbench media storage and realtime

Status: **proposed** · Bead: `agent-forge-harness-1kg.1.4` · Date: 2026-09-17
Implements: [`gm-workbench-interactions.md`](gm-workbench-interactions.md) — X-4,
TABLE-6, TABLE-7, TABLE-11, REVEAL-17, REVEAL-21, REVEAL-24, REVEAL-25,
AUDIO-11 to AUDIO-17, AUDIO-24 to AUDIO-28 · Bound by:
[`gm-workbench-threat-model.md`](gm-workbench-threat-model.md) — SEC-6, SEC-7,
SEC-9, SEC-13, SEC-15, SEC-16, SEC-19, SEC-25 to SEC-31, SEC-35 · Sibling: the
Live Session Assistant's transport decision (`agent-forge-harness-1ir.1.11`),
which rides this one · Master plan:
[`../forge/plans/aetheril-gm-workbench-expansion.md`](../forge/plans/aetheril-gm-workbench-expansion.md)

Two things the Workbench needs that the repository has no shape for: a place for
the bytes of a portrait, a map or a cue, and a way for a change made on one
Cloud Run instance to reach every GM tab and every player's phone. This document
decides both, from the platform as it is deployed today, and within the rules the
threat model fixed. It does not define wire shapes (`1kg.1.2`), the migration
runner, the connection pool or the outbox table (`1kg.1.5`), or the routes
themselves (their beads); section 8 says what each of those must now do.

## 1. How to read this

- **`MS-n`** — a fixed rule about media storage. **`RT-n`** — a fixed rule about
  realtime delivery. Downstream beads implement them; changing one means amending
  this document.
- **`F-n`** — a fact about the repository or the platform, verified on the date
  above.
- **`LT-n`** — a load-test target. The threat model's T-15 is LT-1 to LT-5.
- **`Q-n`** — a question for the owner, each with a default in force.
- Decision IDs without a prefix (`X-4`, `REVEAL-25`) are the interaction record's;
  `SEC-n`, `WT-n` and `T-n` are the threat model's.

Every rule carries the same provenance tags as the two documents it follows:

| Tag | Meaning |
| --- | --- |
| **S** | Supplied by the design handoff |
| **R** | Forced by the repository or the platform as they are today |
| **P** | Fixed by the master plan, a bead's acceptance criteria, the interaction record or the threat model |
| **I** | Inferred here — a judgement, and the default in force until changed |
| **E** | Escalated to the owner (section 7) |
| **N** | An intentional non-goal |

Numbers marked *suggested* are defaults a downstream bead may tune with evidence
from the load tests in section 5. A rule's requirement is fixed; a suggested
number is not.

## 2. What is true today

Verified in the repository and the deployment scripts on 2026-09-17.

| # | Fact | Where | Consequence |
| --- | --- | --- | --- |
| F-1 | Production is **one Cloud Run service, one container, one uvicorn process**: `--concurrency 20 --max-instances 2 --timeout 300 --memory 1Gi`, no minimum instances, no VPC connector. | `scripts/deploy.sh`, `Dockerfile.cloud` | Forty request slots serve everything. An open stream holds one slot for its life and is cut at 300 s (TABLE-6 already assumes it). An idle service scales to zero; a live table keeps it warm. |
| F-2 | **Every endpoint is synchronous** (`def`), run in the framework's thread pool of 40 threads. | `service/app.py` | A stream served by a synchronous handler would pin a thread for its life. Streams and byte serving are `async`, and the pool keeps serving chat. |
| F-3 | Database access **opens a psycopg connection per operation; there is no pool**. Cloud SQL is `db-f1-micro`, 10 GB HDD, zonal — a shared-core instance whose connection ceiling is small. | `service/history.py:176`, `service/auth_store.py`, `docs/deploy-gcp.md` §3 | Forty concurrent requests can already exceed that ceiling; realtime adds one listening connection per instance. A bounded pool is `1kg.1.5`'s and this decision depends on it (RT-5). |
| F-4 | The schema is idempotent DDL re-applied at every startup. | `service/schema.py`, `service/sql/` | Ordered migrations, the outbox table and the job runner are `1kg.1.5`'s; this document says what rides on them. |
| F-5 | **No image, audio or object-storage library is in the image**: no Pillow, no ffmpeg, no `google-cloud-storage`. The image is `python:3.12-slim` plus the built UI. | `pyproject.toml`, `Dockerfile.cloud` | Processing tooling is new weight in the image (MS-6, Q-2). |
| F-6 | Attachments keep extracted text and discard bytes; the largest body the proxy allows is 4 MB of base64 JSON. | `service/attachments.py`, `ui/nginx.conf` | There is no precedent for storing or serving bytes, and base64 JSON is the wrong vehicle for them (MS-5). |
| F-7 | In Compose and E2E, nginx proxies six named locations with **default buffering**, a 120 s read timeout on two of them and a 4 MB body cap on one; production has no nginx. | `ui/nginx.conf`, `docker-compose*.yml` | Every new route family needs a location; a stream needs buffering off; uploads need caps; and the E2E posture must match production (threat model R-6). |
| F-8 | Cloud Run's front end negotiates HTTP/2 with browsers and speaks HTTP/1 to the container unless `--use-http2` is set; nginx in Compose serves HTTP/1.1. | Cloud Run documentation; `ui/nginx.conf` | The browser's six-connections-per-host limit applies only in E2E, where the two streams a page opens fit. |
| F-9 | Cloud Run sends SIGTERM and allows 10 s; instance affinity is best-effort; an HTTP/1 request body is capped at 32 MiB. | Live Session plan, hosting research | Streams end gracefully on shutdown (RT-3); nothing may depend on which instance holds a stream (RT-5); the largest upload, 20 MB, fits (MS-5). |
| F-10 | The project already has the Pub/Sub API enabled for the budget kill-switch and Secret Manager for secrets. | `docs/deploy-gcp.md` §4, §5 | A bucket and one IAM binding are the only new infrastructure (MS-1). Pub/Sub exists but is not the fanout (RT-5). |
| F-11 | Every store follows one pattern: a `Protocol`, an in-memory implementation for unit tests, a Postgres one for the service, chosen at startup. | `service/history.py`, `service/auth_store.py`, `service/app.py:lifespan` | The object store follows it (MS-10). |
| F-12 | The GM session is the stateless cookie of R-1; table credentials are the threat model's, `Path=/table`, `SameSite=Strict`. | `service/session.py`, SEC-5, SEC-6 | A stream is authenticated by the cookie a same-origin `EventSource` sends, and re-checked per frame (RT-7). |

## 3. Where bytes live

### 3.1 Options

| Option | Same-origin, slot-authorised reads (SEC-16)? | Cost | Operations | Verdict |
| --- | --- | --- | --- | --- |
| **Cloud SQL `bytea`** | Yes, through the service | Storage on a 10 GB HDD instance already holding the corpus; every backup and point-in-time log carries the media | Range requests need chunking in SQL; a 20 MB cue is a 20 MB row | **Rejected.** The database is the smallest, most expensive place to put bytes, and SEC-36's deletion promise would ride on backup retention |
| **Cloud Storage, private bucket, served through the service** | Yes | Cents per GB-month; egress from Cloud Run to players; instance CPU while streaming | One bucket, one IAM binding, a client library | **Chosen** (MS-1 to MS-12) |
| Cloud Storage with signed URLs handed to browsers | No — cross-origin, and a URL is a bearer secret that reaches a player's history, cache and proxy logs | Cheapest egress | Simplest serving | **Rejected** by SEC-16, X-10 and AUDIO-30; also lets two slots be correlated by URL |
| The instance's filesystem | Yes | Free | Memory-backed, per instance, gone at scale-down | **Rejected.** Not durable and not shared |
| A media host or CDN with its own accounts | No | Monthly fee | Another trust boundary that would hold private images | **Rejected** (SEC-20: private text lives in the database and the provider request only) |

### 3.2 Rules

| ID | Rule | Basis |
| --- | --- | --- |
| MS-1 | **One private regional bucket per environment**, in the service's region (`us-central1`), with uniform bucket-level access, public access prevention enforced, no `allUsers` or `allAuthenticatedUsers` grant, and no listing for anyone but the runtime service account, which holds object read/write/delete on **that bucket only**. The bucket has no versioning, and the soft delete new buckets receive by default is turned off, so that deleted means deleted (SEC-36). | P (SEC-29) + I |
| MS-2 | **Object keys are random and say nothing**: `assets/<32 hex>` for a processed original and `assets/<32 hex>/<derivative>` for what `1kg.8.2` derives; uploads in flight live under `tmp/<32 hex>`. No key carries a campaign, a document, a cue name or a filename (SEC-28); the asset row is the only map from owner to key. | P (SEC-28) + I |
| MS-3 | **An asset has five states**, each a row update in a transaction: `uploading` (bytes streaming to a `tmp/` key) → `processing` → `ready` (the processed object written, `tmp/` deleted) or `failed` (`tmp/` deleted) → `deleted`. Deleting writes the tombstone and the outbox job that removes the objects **in the same transaction**; the job is idempotent and retried until the bucket agrees; the row is purged once it has. A bucket lifecycle rule deletes any `tmp/` object older than one day, as the backstop for an instance that died mid-upload (SEC-37). No reference to an asset resolves — for the GM or a table — unless its state is `ready` (SEC-27). | P (SEC-27, SEC-29, SEC-37) + I |
| MS-4 | **Quotas are checked before bytes are accepted** (SEC-31): *suggested* 500 MB and 500 assets per campaign. An upload must declare `Content-Length` (411 otherwise); the declared size is reserved against the quota in the transaction that creates the `uploading` row, and replaced by the real size when processing ends. | P (SEC-31) + I |
| MS-5 | **An upload is one raw body per request**: `PUT` or `POST` with the file's declared media type as `Content-Type` (SEC-7's upload exception), never multipart and never base64 JSON (F-6). The body is **streamed** to the `tmp/` key while the service counts bytes; the cap of SEC-26 is applied to the count, ignoring the header, and the proxy caps the route at the same number (F-7). The largest cap, 20 MB, is under the platform's 32 MiB (F-9). | P (SEC-26) + R + I |
| MS-6 | **Processing runs in a subprocess, one at a time per instance** (a semaphore of one, so two uploads queue instead of doubling memory), with a wall clock of 60 s and an address-space limit of 512 MB (*suggested*, SEC-27), kept off the network by construction — file and pipe protocols only, a decoder that cannot fetch (SEC-27) — with a scratch directory deleted afterwards and the header checks of SEC-26 (25 megapixels, 8,192 px a side) before any decode. **Images** are read by Pillow: the header first (dimensions and pixel count against the caps, with `Image.MAX_IMAGE_PIXELS` set to the same cap as a second guard), then decoded and re-encoded within the same family — PNG to PNG, JPEG to JPEG, WebP to WebP — with no EXIF, no ICC profile, no ancillary chunks, alpha kept where the family has it. **Audio** is probed first (exactly one audio stream, no video stream, duration against the cap) and transcoded by ffmpeg, `-nostdin`, file and pipe protocols only, every container tag, chapter and attached picture dropped (`-map_metadata -1 -map_chapters -1 -vn`, SEC-27), to **MP3** (LAME, VBR around 130 kbps): universally playable, seekable by byte range, no container atoms to keep in order. ffmpeg is installed in both images from Debian's package, which is the price of not running a second service for the pilot (Q-2). Derivatives — thumbnails, waveform peaks, duration — are `1kg.8.2`'s and are made from the processed original, never from the upload. | P (SEC-25 to SEC-27) + I |
| MS-7 | **Bytes are served same-origin, by the service, from an async handler** (F-2): `GET /assets/{asset_id}` for the GM (session cookie, ownership through the campaign, SEC-2) and `GET /table/assets/{handle}` for a table client (table cookie, then the slot, SEC-16). `Range` is honoured (`206`, `Accept-Ranges: bytes`), because a cue seeks. Bytes stream from the object store in chunks of 256 KiB (*suggested*), so a slow phone holds a slot, not a thread and not the file. Headers are SEC-19's: the server-recorded type, `nosniff`, `Content-Disposition` without a filename, `Cache-Control: no-store` for the GM and the table alike, and no `ETag` (SEC-19). A response longer than 1 MB re-checks its slot every 1 MB and stops at the first failure (SEC-16); a shorter one completes — a second of bytes the client was entitled to when it started — and the next request for it is refused. | P (SEC-16, SEC-19) + R + I |
| MS-8 | **A table client names an asset by a per-slot handle** (SEC-15): 128 random bits, minted in the transaction that makes a slot show the asset, resolved handle → slot → live session and current generation → recipient entitled → object key, and dead with its slot. Two slots showing one portrait hold two handles; a handle never carries a title, a filename or the GM-side `asset_id`. | P (SEC-15) + I |
| MS-9 | **Import by URL stays off** (`WORKBENCH_IMPORT_BY_URL`, default off) and, when on, is SEC-30 exactly: the service resolves the name itself, checks every address, connects only to a checked address with the original host name for TLS, follows at most three redirects each checked the same way, caps the stream by count, writes to `tmp/` and hands the object to MS-6. The processing subprocess never has the network; the fetch is the service's. | P (SEC-30, AUDIO-25) |
| MS-10 | **One `ObjectStore` protocol, three implementations** (F-11): in memory for unit tests; a directory under `WORKBENCH_MEDIA_DIR` for Compose and E2E (a named volume, so a restart keeps it); Cloud Storage for production, through `google-cloud-storage` and the runtime service account's default credentials. The store is chosen at startup the way `DATABASE_URL` chooses Postgres today. One contract test suite runs against all three — the Cloud Storage one against `fake-gcs-server` in the integration profile, and against a real bucket where credentials exist. The Compose image installs the same ffmpeg and Pillow as production, so E2E exercises the real pipeline (R-6). | R + P (the plan's integration profile) + I |
| MS-11 | **Cost has four drivers and one guard.** Storage is cents per GB-month at the quotas of MS-4. Egress is the real one: table responses are `no-store` (SEC-13), so a 5 MB portrait revealed to six phones moves 30 MB, and a 20 MB ambience pushed to six moves 120 MB; a busy pilot campaign moves a few GB a month, which is cents. Instance CPU while streaming, and the larger image's cold start, are the rest. The guard is the existing budget alert (F-10) plus MS-4's quotas plus a **per-campaign monthly egress budget** (*suggested* 20 GB; the number is `yje`'s): over it, media for that campaign stops serving until the next UTC day, the GM is told plainly, and a table client simply sees no image (Q-4). | I · **E** (Q-4) |
| MS-12 | **Media is not backed up in the pilot.** The database backups do not contain it, the bucket has no versioning and no soft delete (MS-1), and a bucket loss loses it; the runbook says so. Deletion is therefore immediate and complete, which is what SEC-36 promises. Revisit before the Workbench leaves the pilot (Q-3). | I · **E** (Q-3) |

## 4. How a change reaches every browser

### 4.1 Options

Two questions, decided separately: the **transport** to a browser, and the
**fanout** between instances.

| Transport | Fits | Verdict |
| --- | --- | --- |
| **Server-sent events** (`text/event-stream` over the ordinary origin) | One direction is all the Workbench needs: every command is a POST (REVEAL-22, AUDIO-24). Native `EventSource` sends the cookie, reconnects by itself, and needs no upgrade to check an `Origin` on. Streams through Cloud Run's front end and, with buffering off, through nginx. | **Chosen** (RT-1) |
| WebSocket | Bidirectional, which nothing here uses; a cross-site upgrade rides the cookie unless `Origin` is checked (SEC-7); an open socket bills its instance for its whole life and is cut at the same 300 s. | Rejected for v1; nothing in the protocol below forbids it later |
| Long polling | Works everywhere, but every poll is a request slot and a database read, and a 10 s silence grace (X-4) means polling at 3–5 s per client. | Rejected; kept as the client's degraded mode only if a stream cannot open (RT-9) |
| A push provider (Pusher, Ably, Firebase) | A third party receives event metadata and holds the connections; a second trust boundary and a monthly fee. | Rejected (SEC-20) |

| Fanout | Fits | Verdict |
| --- | --- | --- |
| **Postgres: the outbox as the record, `LISTEN`/`NOTIFY` as the wake-up** | State, the change and the notification commit together; a missed notification is recovered by reading the outbox; no new infrastructure; one listening connection per instance. | **Chosen** (RT-5) |
| Pub/Sub | Push subscriptions need a public endpoint with its own authentication; pull needs a background thread per instance; a second system to reason about, for no gain at two instances. | Rejected for the pilot; the API stays enabled for the budget kill-switch (F-10) |
| Memorystore (Redis) pub/sub | Needs a VPC connector and a paid instance — more than the pilot's whole budget — and adds a second store of truth. | Rejected |
| Sticky sessions | Cloud Run affinity is best-effort (F-9), and a GM's two tabs and a player's phone would have to share an instance. | Rejected (AUDIO-24) |

### 4.2 Rules

| ID | Rule | Basis |
| --- | --- | --- |
| RT-1 | **The transport is server-sent events, from async handlers, and every command is a POST.** Two streams: the GM channel, `GET /campaigns/{campaign_id}/stream`, authenticated by the session cookie and ownership (SEC-2); the table channel, `GET /table/stream`, authenticated by the table credential alone (SEC-1), the session coming from the credential. The client is the native `EventSource`. A stream never mutates anything, so it needs no origin check; the POSTs that do are SEC-7's. There is no WebSocket in v1, so SEC-7's upgrade clause has nothing to apply to. | P (AUDIO-24, REVEAL-24) + R + I |
| RT-2 | **A frame is one event of the wire contract's realtime family**: `event:` names the kind, `data:` is one JSON object with `schema_version`, the slot, its sequence and the link generation it was produced under, and never more text than the projection allows (SEC-15). The heartbeat is an SSE **comment** line every 5 s (*suggested*, TABLE-7) — not an event, nothing to parse, and it keeps the grace of X-4 running from the last byte. `retry:` is 1 s. The `id:` field is not used: resumption is by snapshot (RT-4), not by `Last-Event-ID`. | P (TABLE-7, SEC-15) + I |
| RT-3 | **A stream is bounded in time.** The server closes every stream after 280 s (*suggested*, under the platform's 300 s, F-1) with a final `reconnect` event, and on SIGTERM (F-9) the same way; the client reopens at once. A request slot is therefore never held longer than 280 s, a reconnect is routine rather than an error (TABLE-6), and an instance being replaced is invisible to a table. | R + P (TABLE-6) + I |
| RT-4 | **Snapshot, then events** (AUDIO-15). Every stream begins with a `snapshot`: the session's state, then each slot the recipient is entitled to with its content and sequence — for a table client, the table slot and its own participant slot only (REVEAL-24); for the GM, every slot with the epoch. Events follow with sequences above the snapshot's; the client keeps a high-water mark per slot, drops anything at or below it, and on a gap closes the stream and reopens, which yields a fresh snapshot. Hiding, freezing or reconnecting blanks content and resets the marks (TABLE-7, TABLE-11). Nothing a client held before a reconnect is trusted afterwards. | P (AUDIO-15, TABLE-7, TABLE-11) |
| RT-5 | **Postgres is the bus.** The transaction that changes a slot — reveal, update, replace, clear, Stop, push, End, Rotate, a presence roll-up — does three things atomically: writes the state with `seq = seq + 1 RETURNING seq`, inserts an **outbox** row (session, slot, sequence, generation, event kind, ids only, no text) and `NOTIFY` on the session's channel, which Postgres delivers on commit. Each instance runs one **listener** task on a dedicated connection outside the pool (F-3): on a notification it reads the outbox rows past its cursor for that session and fans them out to its local subscribers; every 2 s (*suggested*) it reads past its cursors regardless, so a missed notification — the listener reconnecting, a restart — costs at most 2 s, never an event. A subscriber never re-renders from the payload alone: the instance reads the changed slot's projection once per event and hands each local stream the part its recipient is entitled to. The outbox table, its cursors, the job runner and the pool are `1kg.1.5`'s; the listener's connection, the channel names and the 2 s poll are this rule's. Event rows are pruned after 24 h (*suggested*); deletion jobs (MS-3) ride the same table with a kind of their own and are claimed with `FOR UPDATE SKIP LOCKED`. | P (AUDIO-24) + R + I |
| RT-6 | **Ordering and idempotency.** A sequence is assigned by the database in the changing transaction, so it is total per slot; across the bus an event may still arrive late or twice, and RT-4's marks make that harmless. A command carries its `command_id` — scoped to the caller (wire contract, Idempotency) — and the epoch it was issued under (REVEAL-22, AUDIO-28): a stale epoch is `409 conflict`, a replayed id returns the first outcome and does nothing, and the server orders commands by the transaction that commits them, never by arrival. A Stop carries no epoch and is never queued (X-3). | P (REVEAL-22, AUDIO-28, X-3) + I |
| RT-7 | **Entitlement is checked per frame, without a database round trip per frame** (SEC-9). Every outbox row carries the link generation it was produced under; a stream holds its credential's generation; a frame whose generation differs is not written and the stream is closed. End and Rotate produce a `session` event under the new generation, so every old-generation stream on every instance closes within one bus delivery of the transaction, and no frame of the new generation ever reaches a stream opened under the old one (T-5). A participant slot's frames go only to streams whose credential pair names that participant (SEC-11); a guest's transcript is the same whether or not a private reveal happened (T-8). The GM channel checks ownership once at open and closes when the campaign is deleted or the account loses `dm` (R-2 makes the next command fail either way). | P (SEC-9, SEC-11, REVEAL-24) + I |
| RT-8 | **Connections are bounded and counted where they can be seen.** Each open stream is a row in a `connections` table — session, credential, instance, opened, last seen — refreshed by the stream every 15 s (*suggested*) and considered dead after 45 s; the row is what REVEAL-25's per-session (12) and per-device (3) bounds are checked against on connect, across instances, and what presence is aggregated from (AUDIO-24), so the GM's device count is the truth and not one instance's view. The **per-instance** ceiling is enforced in memory: `WORKBENCH_STREAMS_PER_INSTANCE`, *suggested* 10 with the concurrency of 20 — half of an instance's slots, as REVEAL-25 requires — of which 2 are reserved for GM channels, so a full table never locks its own GM out. Beyond the credential bound the join itself answers `full` (SEC-10); beyond a connection bound the stream request is refused with a 503 and a `Retry-After`, the client shows `This table is full` (REVEAL-25) and retries with backoff. At the pilot's settings that is one full table and most of a second (Q-1). | P (REVEAL-25, AUDIO-24, SEC-35) + R + I |
| RT-9 | **Health and degradation are explicit.** `/healthz` gains `realtime: {listener: listening or reconnecting, streams: n, cap: m}` and `media: {store: ok or unavailable}`; a listener that is reconnecting is `degraded`, not `down`. Listener down: streams keep working from the 2 s poll (RT-5) with up to 2 s more latency. Object store unavailable: uploads and asset reads answer `503 backend_unavailable` (GM) or fail generically (table); text reveals and audio state are unaffected. Database unavailable: everything fails closed, as today. At the stream cap: `This table is full`. Instance shutdown: `reconnect`, then the client is on another instance. If a stream cannot be opened at all, the table client polls its snapshot every 5 s (*suggested*) and says so with the `Reconnecting…` banner of TABLE-6; polling is a degraded mode, never the design. | P (the bead's AC) + I |
| RT-10 | **Proxy and platform settings** (`1kg.9.5`): Cloud Run `--timeout 300` unchanged and no session affinity; `--concurrency` per Q-1. nginx, for the two stream locations: `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 320s`, `proxy_http_version 1.1`, `proxy_set_header Connection ""`; the service also sends `X-Accel-Buffering: no` on every stream. For upload locations: `client_max_body_size` equal to the route's SEC-26 cap and `proxy_request_buffering off`, so bytes stream to the service and MS-5's count is real. The E2E stack runs streams and uploads through nginx, so a wrong setting fails in CI and not in a session (R-6, T-17). | R + P (R-6) + I |
| RT-11 | **What realtime costs**: a request slot per open stream for at most 280 s at a time; an instance kept warm while any stream is open (a live table is a live service, which is right); a few bytes of heartbeat every 5 s; one `connections` write every 15 s per stream; one `NOTIFY`, one outbox row and one read per instance per change. No new billable infrastructure. | I |
| RT-12 | **The GM channel is the same mechanism with the GM's entitlements**: lane status for invocations and edits, reveal and audio state with the epoch and every slot, presence with aliases and guest counts (threat model 8.3). Two GM tabs are two streams that converge through the same events (AUDIO-24); a GM without a stream still sees every state on the next read, because the stream carries nothing the resources do not. | P (AUDIO-24, 8.3) |
| RT-13 | **The scale path changes settings, not the protocol.** Past the pilot, streams move to a second Cloud Run service built from the same image — `--concurrency 250`, async routes only, the same `/table/stream` and `/campaigns/{id}/stream` paths behind a URL-map load balancer on the same origin — and the request-serving service keeps its slots for login, chat and tools. Because the bus is the database, nothing in RT-1 to RT-9 changes. Not in v1: a load balancer costs more per month than the pilot's whole budget (Q-1). | I · **E** (Q-1) |
| RT-14 | **The Live Session Assistant rides these streams.** Capture state reaches table clients as booleans only, on the table channel, under RT-4's snapshot rule; the GM's capture controls are POSTs; there is no second channel and no second fanout (`agent-forge-harness-1ir.1.11`). | P (`1ir.1.11`) |

## 5. Load-test targets

These are the threat model's T-15, made concrete. `1kg.9.5` chooses the tool and
runs them against a Compose stack of two service replicas behind nginx and
against a Cloud Run staging deploy at the pilot settings. A target that fails
moves a *suggested* number in this document, with the measurement beside it.

| # | Target | Proves |
| --- | --- | --- |
| LT-1 | With 20 table streams open and heartbeating across two sessions, 20 concurrent `/chat` requests and 10 logins complete with p95 latency within 1.5× their no-stream baseline, and the 21st stream is refused with `This table is full` within 200 ms. | SEC-35, REVEAL-25, RT-8 |
| LT-2 | A Stop reaches every entitled stream across both instances within 1 s at p95 and 2.5 s at worst (X-3, AUDIO-13's 3 s). | RT-5, RT-7 |
| LT-3 | A push is audible on 12 players of one session within 3 s at p95, measured from the POST to the first byte of media fetched (AUDIO-13). | RT-5, MS-7 |
| LT-4 | 20 streams reconnecting at once at the 280 s boundary complete their snapshots within 2 s at p95, and chat p95 stays within LT-1's bound throughout. | RT-3, RT-4 |
| LT-5 | A 10 MB image and a 20 MB ambience each process within 15 s at p95 with one job at a time; two concurrent uploads queue and both succeed; instance memory stays under 900 MiB throughout. | MS-6 |

## 6. What the wire contract must carry (`1kg.1.2`, media and realtime families)

The shapes and their fixtures are the contract's; this is what they must be
able to say.

- **A GM-side asset**: `asset_id`, `kind` (`image` or `audio`), `state`
  (`uploading`, `processing`, `ready`, `failed`), the declared and then the
  server-recorded `media_type`, size in bytes, `width` and `height` or
  `duration_ms` once ready, alt text for an image and no text for audio, a
  closed failure reason, and timestamps. Step one of an upload is a JSON create
  request answered with the asset in `uploading`; step two is the raw bytes;
  nothing resolves until `ready` (MS-3). The existing `AssetRef` — an id, a
  type, an `alt` — stays the reference a document field holds.
- **A table-side asset reference** is the per-slot handle of MS-8 with `kind`,
  `media_type`, `width`/`height` or `duration_ms`, and nothing else: no id, no
  title, no filename (SEC-15).
- **Realtime events** share one envelope — `schema_version`, `event`, `slot`,
  `seq`, `gen` — with these kinds: `snapshot`, `slot` (content), `clear`, `audio`
  (play or stop: cue handle, `started_at`, start offset, loop flag, duration),
  `session` (ended or rotated; a table client receives one generic inactive
  event), `presence` and `lane` (GM channel only), and `reconnect`. The heartbeat
  is a comment, not an event. A table event carries only what the projection of
  its slot allows.
- **Commands** are POST bodies with a caller-scoped `command_id` and the epoch they
  were issued under, except a Stop, which carries none (RT-6).
- **Join** answers carry the bounds' outcome: joined with the device's role, or
  `full` (RT-8), still with no reason for a failed token (SEC-8). A refused
  stream is an HTTP status, not a frame.

## 7. Questions for the owner

Nothing here blocks the work. Each has a default in force.

| # | Question | Default in force | It matters because |
| --- | --- | --- | --- |
| Q-1 | Raise Cloud Run `--concurrency` from 20 to 40 so that async streams use request slots the 40-thread pool cannot, with the stream ceiling raised to 24 per instance? | **Not until LT-1 passes at 20**; then yes, re-running LT-1 at 40. The load balancer topology of RT-13 is not for the pilot. | At 20 the pilot serves one full table and most of a second at once; at 40, two and a half. |
| Q-2 | Install ffmpeg in the production image (roughly a hundred megabytes and a slower cold start) to transcode audio in-process? | **Yes**, one job at a time (MS-6). The alternatives — storing audio as uploaded, or a processing service — fail SEC-27 or cost a second service. | Cold start on scale-from-zero grows by seconds; a live table keeps the service warm anyway. |
| Q-3 | Accept that media has no backup in the pilot (MS-12)? | **Yes** for the pilot; revisit in `1kg.9.6` before leaving it. | A bucket loss loses portraits and cues; documents are unaffected. |
| Q-4 | A per-campaign monthly egress budget, and media pausing for that campaign when it is spent (MS-11)? | **Yes**, at 20 GB, until `yje` sets real numbers. | Without it one leaked link and a script can turn a 20 MB ambience into the month's bill. |

## 8. What this changes elsewhere

### 8.1 Amendments the interaction record needs

None reverses a decision; each closes a choice the record left to this bead.

1. **REVEAL-21** — the choice is made: table media is served same-origin by the
   service (MS-7); the 60 s signed-read alternative is not taken.
2. **AUDIO-24** — the mechanism is chosen: Postgres-held state, a transactional
   outbox, `LISTEN`/`NOTIFY` as the wake-up, per-instance fanout (RT-5).
3. **TABLE-6** — the platform's 300 s cut becomes a deliberate 280 s server-side
   close with a `reconnect` event (RT-3); the banner rule is unchanged.
4. **REVEAL-25** — the bounds are numbers now: 12 per session, 3 per device, 10
   streams per instance of which 2 are the GM's, counted in a `connections`
   table across instances (RT-8).
5. **AUDIO-26** — audio is re-encoded to MP3 and images within their family; the
   formats accepted are unchanged (MS-6).

### 8.2 What this settles in the threat model

- SEC-7's WebSocket clause has nothing to apply to in v1 (RT-1).
- SEC-16's bounded alternative — single-asset reads of at most 60 s — is not
  taken; every table read is slot-authorised (MS-7, MS-8).
- SEC-26 and SEC-27's *suggested* ceilings and limits are adopted as numbers
  (MS-5, MS-6).
- Its SEC-16 chunked re-check is MS-7's 1 MB; its SEC-27 bound on processing is
  MS-6's semaphore of one and 512 MB.

### 8.3 Beads this refines

| Bead | What it must now do |
| --- | --- |
| `1kg.1.5` | A bounded pool with clean shutdown (F-3); the outbox table with event rows, deletion jobs and per-instance cursors; a job runner using `FOR UPDATE SKIP LOCKED`; the `connections` table; one dedicated listener connection outside the pool (RT-5, RT-8) |
| `1kg.1.2` | The media and realtime families of section 6 |
| `1kg.2.3` | Join answers the bounds (RT-8); `connections` rows are how presence and device counts are read |
| `1kg.7.5` | RT-1 to RT-9 for the table channel: snapshot-then-events, per-slot marks, the 280 s close, the generation check, polling as the degraded mode |
| `1kg.8.1` | MS-1 to MS-11: the bucket, the store protocol and its three implementations, the streaming upload, the processing subprocess, the asset routes, import behind its flag |
| `1kg.8.2` | Derivatives from the processed original only, states of MS-3 |
| `1kg.8.6` | Audio slots on the bus: `started_at` on the server's clock, sequence and epoch per RT-5 and RT-6 |
| `1kg.9.2` | The `/healthz` fields and the stream, listener, upload and egress metrics with closed labels (RT-9, SEC-22) |
| `1kg.9.5` | The bucket and its IAM binding, ffmpeg and Pillow in both images, nginx locations and settings (RT-10), Cloud Run flags (Q-1), LT-1 to LT-5, and the runbooks: bucket loss, orphan reconciliation, listener health, quota and egress overrides |
| `1kg.9.6` | Q-3 and Q-1 as rollout gates |
| `agent-forge-harness-1ir.1.11` | Rides RT-1 and RT-14; capture state is booleans on the table channel |
| `agent-forge-harness-yje` | The quota and egress numbers of MS-4 and MS-11 |

## 9. Review log

Not yet reviewed. This section records each review, its findings and what was
done about them, as the interaction record's section 18 does.
