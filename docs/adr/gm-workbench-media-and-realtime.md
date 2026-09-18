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
| F-1 | Production is **one Cloud Run service, one container, one uvicorn process**: `--concurrency 20 --max-instances 2 --timeout 300 --memory 1Gi`, no minimum instances, no VPC connector, **CPU allocated only while a request is in flight** (the default; always-on CPU is not bought), and the autoscaler adds an instance when one is about 60 % busy — twelve of twenty in flight. | `scripts/deploy.sh`, `Dockerfile.cloud`, Cloud Run documentation | Forty request slots serve everything. An open stream holds one slot for its life and is cut at 300 s (TABLE-6 already assumes it). Nothing runs between requests (RT-15). An idle service scales to zero; a live table keeps it warm. A per-instance refusal below twelve in flight would never let the second instance start (RT-8). |
| F-2 | **Every endpoint is synchronous** (`def`), run in the framework's thread pool of 40 threads. | `service/app.py` | A stream served by a synchronous handler would pin a thread for its life. Streams and byte serving are `async`, and the pool keeps serving chat. |
| F-3 | Database access **opens a psycopg connection per operation; there is no pool**. Cloud SQL is `db-f1-micro`, 10 GB HDD, zonal — a shared-core instance that allows 25 connections, 22 of them for the application. | `service/history.py:176`, `service/auth_store.py`, `docs/deploy-gcp.md` §3 | Forty concurrent requests can already exceed that ceiling; realtime adds a listening connection and a small async pool per instance. Bounded pools are `1kg.1.5`'s and this decision depends on them: *suggested* 6 sync, 3 async and the listener per instance, 20 in all (RT-5). |
| F-4 | The schema is idempotent DDL re-applied at every startup. | `service/schema.py`, `service/sql/` | Ordered migrations, the outbox table and the job runner are `1kg.1.5`'s; this document says what rides on them. |
| F-5 | **No image, audio or object-storage library is in the image**: no Pillow, no ffmpeg, no `google-cloud-storage`. The image is `python:3.12-slim` plus the built UI. | `pyproject.toml`, `Dockerfile.cloud` | Processing tooling is new weight in the image (MS-6, Q-2). |
| F-6 | Attachments keep extracted text and discard bytes; the largest body the proxy allows is 4 MB of base64 JSON. | `service/attachments.py`, `ui/nginx.conf` | There is no precedent for storing or serving bytes, and base64 JSON is the wrong vehicle for them (MS-5). |
| F-7 | In Compose and E2E, nginx proxies six named locations with **default buffering**, a 120 s read timeout on two of them and a 4 MB body cap on one; production has no nginx. | `ui/nginx.conf`, `docker-compose*.yml` | Every new route family needs a location; a stream needs buffering off; uploads need caps; and the E2E posture must match production (threat model R-6). |
| F-8 | Cloud Run's front end negotiates HTTP/2 with browsers and speaks HTTP/1 to the container unless `--use-http2` is set; nginx in Compose serves HTTP/1.1. | Cloud Run documentation; `ui/nginx.conf` | The browser's six-connections-per-host limit applies only in E2E, where the two streams a page opens fit. |
| F-9 | Cloud Run sends SIGTERM and allows 10 s; instance affinity is best-effort; an HTTP/1 request body is capped at 32 MiB. | Cloud Run documentation (termination, session affinity, request limits) | Streams end gracefully on shutdown (RT-3); nothing may depend on which instance holds a stream (RT-5); the largest upload, 20 MB, fits (MS-5). |
| F-10 | The project already has the Pub/Sub API enabled for the budget kill-switch and Secret Manager for secrets. | `docs/deploy-gcp.md` §4, §5 | A bucket and one IAM binding are the only new infrastructure (MS-1). Pub/Sub exists but is not the fanout (RT-5). |
| F-11 | Every store follows one pattern: a `Protocol`, an in-memory implementation for unit tests, a Postgres one for the service. Today `lifespan` always builds the Postgres stores; choosing by environment arrives with `1kg.1.5`'s pool. | `service/history.py`, `service/auth_store.py`, `service/app.py:lifespan` | The object store and the bus follow it (MS-10). |
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
| MS-3 | **An asset has five states**, each a row update in a transaction: `uploading` (bytes streaming to a `tmp/` key) → `processing` → `ready` (the processed object written, `tmp/` deleted) or `failed` (`tmp/` deleted) → `deleted`. Deleting writes the tombstone and the outbox job that removes the objects **in the same transaction**; the job is idempotent and retried until the bucket agrees; the row is purged once it has. A bucket lifecycle rule deletes any `tmp/` object older than one day, as the backstop for an instance that died mid-upload (SEC-37). A row stuck in `uploading` for an hour or in `processing` for ten minutes (*suggested*) is moved to `failed` (`timed_out`) by a sweep job (RT-15), which also releases its quota reservation (MS-4) and its `tmp/` object. No reference to an asset resolves — for the GM or a table — unless its state is `ready` (SEC-27). | P (SEC-27, SEC-29, SEC-37) + I |
| MS-4 | **Quotas are checked before bytes are accepted** (SEC-31): *suggested* 500 MB and 500 assets per campaign. An upload must declare `Content-Length` (411 otherwise); the declared size is reserved against the quota in the transaction that creates the `uploading` row, and replaced by the real size when processing ends. | P (SEC-31) + I |
| MS-5 | **An upload is one raw body per request**: `PUT` or `POST` with the file's declared media type as `Content-Type` (SEC-7's upload exception), never multipart and never base64 JSON (F-6). The body is **streamed** to the `tmp/` key while the service counts bytes; the cap of SEC-26 is applied to the count, ignoring the header, and the proxy caps the route at the same number (F-7). The largest cap, 20 MB, is under the platform's 32 MiB (F-9). | P (SEC-26) + R + I |
| MS-6 | **Processing runs in a subprocess, one at a time per instance** (a semaphore of one, so two uploads queue instead of doubling memory), with a wall clock of 60 s and an address-space limit of 512 MB (*suggested*, SEC-27), kept off the network by construction — file and pipe protocols only, a decoder that cannot fetch (SEC-27) — with a scratch directory deleted afterwards and the header checks of SEC-26 (25 megapixels, 8,192 px a side) before any decode. **Images** are read by Pillow: the header first (dimensions and pixel count against the caps, with `Image.MAX_IMAGE_PIXELS` set to **half** the cap as a second guard — Pillow only warns at that value and raises at twice it), then decoded and re-encoded within the same family — PNG to PNG, JPEG to JPEG, WebP to WebP — with no EXIF, no ICC profile, no ancillary chunks, alpha kept where the family has it. **Audio** is probed first — exactly one audio stream; a video stream is refused unless it is an attached picture, which is what cover art looks like to ffprobe and what `-vn` then drops; duration against the cap — and transcoded by ffmpeg, `-nostdin`, file and pipe protocols only, every container tag, chapter and attached picture dropped and not even the encoder tag written (`-map_metadata -1 -map_chapters -1 -vn -id3v2_version 0 -write_id3v1 0`, SEC-27), to **MP3** (LAME, VBR around 130 kbps): universally playable, seekable by byte range, no container atoms to keep in order. ffmpeg is installed in both images from Debian's package, which is the price of not running a second service for the pilot (Q-2). Derivatives — thumbnails, waveform peaks, duration — are `1kg.8.2`'s and are made from the processed original, never from the upload. **The bytes request stays open until processing ends** — the response is the asset `ready` or `failed` — because processing is request-driven work (RT-15): queueing behind the one job is bounded at 60 s (*suggested*), after which the request answers 503 with `Retry-After` and the asset returns to `uploading` for the retry, so the worst accepted case is 60 s of queue and 60 s of work, under the proxy's upload timeout (RT-10) and Cloud Run's 300 s; twenty concurrent uploads are accepted, queued or told to retry — never lost, never decoded in parallel (LT-5). The GM channel's `asset` frame tells every other tab when the asset is ready (§6). | P (SEC-25 to SEC-27) + I |
| MS-7 | **Bytes are served same-origin, by the service, from an async handler** (F-2): `GET /campaigns/{campaign_id}/assets/{asset_id}` for the GM (session cookie, ownership through the campaign in the same query, SEC-2) and `GET /table/assets/{handle}` for a table client (table cookie, then the slot, SEC-16). **No API route may begin with `/assets/`**: the built UI is mounted at `/` after the API routes and loads its bundle from `/assets/<name>-<hash>.js` (`ui/vite.config.ts` sets no other base), so a route there would capture every bundle request and the app would never load. `Range` is honoured (`206`, `Accept-Ranges: bytes`), because a cue seeks. Bytes stream from the object store in chunks of 256 KiB (*suggested*), so a slow phone holds a slot, not a thread and not the file. Headers are SEC-19's: the server-recorded type, `nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `Content-Disposition` without a filename, `Cache-Control: no-store` for the GM and the table alike, and no `ETag` (SEC-19). A response longer than 1 MB re-checks its slot every 1 MB and stops at the first failure (SEC-16); a shorter one completes — a second of bytes the client was entitled to when it started — and the next request for it is refused. **Byte responses count against capacity**: at most 6 (*suggested*) at once per instance, through the thread limiter the storage client runs on (MS-10), and a seventh answers 503 with `Retry-After` — a phone retries a cue in seconds, and a table's media can never take the request slots login and chat need (SEC-35). LT-1 runs with media fetches in flight. | P (SEC-16, SEC-19, SEC-35) + R + I |
| MS-8 | **A table client names an asset by a per-slot handle** (SEC-15): 128 random bits, minted in the transaction that makes a slot show the asset, resolved handle → slot → live session and current generation → recipient entitled → object key, and dead with its slot. Two slots showing one portrait hold two handles; a handle never carries a title, a filename or the GM-side `asset_id`. | P (SEC-15) + I |
| MS-9 | **Import by URL stays off** (`WORKBENCH_IMPORT_BY_URL`, default off) and, when on, is SEC-30 exactly: the service resolves the name itself, checks every address, connects only to a checked address with the original host name for TLS, follows at most three redirects each checked the same way, caps the stream by count, writes to `tmp/` and hands the object to MS-6. The processing subprocess never has the network; the fetch is the service's. | P (SEC-30, AUDIO-25) |
| MS-10 | **One `ObjectStore` protocol, three implementations** (F-11): in memory for unit tests; a directory under `WORKBENCH_MEDIA_DIR` for Compose and E2E (a named volume, so a restart keeps it); Cloud Storage for production, through `google-cloud-storage` and the runtime service account's default credentials. That client is **synchronous**, so its reads and writes run in a dedicated thread limiter of 6 (*suggested*) — never on the event loop, and never in the request pool F-2 protects — which is also MS-7's media ceiling. The store is selected by an environment variable at startup, the way the pools will be once `1kg.1.5` exists. **The bus follows the same pattern**: an in-process `InMemoryBus` for unit tests and the E2E app (one instance and no Postgres — `docker-compose.e2e.yml` runs `service.e2e_app` on in-memory stores), the Postgres bus for the Compose integration profile and production. One contract test suite runs against every implementation — the Cloud Storage one against `fake-gcs-server` in the integration profile, and against a real bucket where credentials exist. The Compose image installs the same ffmpeg and Pillow as production, so E2E exercises the real pipeline (R-6). | R + P (the master plan's testing strategy: a Postgres, object and realtime integration profile) + I |
| MS-11 | **Cost has four drivers and one guard.** Storage is cents per GB-month at the quotas of MS-4. Egress is the real one: table responses are `no-store` (SEC-13), so a 5 MB portrait revealed to six phones moves 30 MB, and a 20 MB ambience pushed to six moves 120 MB; a busy pilot campaign moves a few GB a month, which is cents. Instance CPU while streaming, and the larger image's cold start, are the rest. The numbers matter more than the drivers: the pilot runs under a **hard $10 monthly cap whose Cloud Function detaches billing at 100 %** (`docs/deploy-gcp.md` §5), and Cloud SQL alone is about $9.40 of it, which leaves cents — Q-4's egress at 20 GB would be about $2.40 and would end the month, and the whole service with it, mid-session. That is why every media capability is **off by default** (the registry's `capabilities`, the same posture as S-4): switching one on is a budget decision before it is a product decision (Q-5), and until the cap is raised the **per-campaign monthly egress budget** is 3 GB (*suggested*; about $0.36) — over it, media for that campaign stops serving until the next UTC day, the GM is told plainly, and a table client simply sees no image (Q-4). RT-9 lists what a detached billing account looks like. | R + I · **E** (Q-4, Q-5) |
| MS-12 | **Media is not backed up in the pilot.** The database backups do not contain it, the bucket has no versioning and no soft delete (MS-1), and a bucket loss loses it; the runbook says so. Deletion is therefore immediate and complete — SEC-36's rule, without the backup window SEC-36 documents, because media is in no backup. Revisit before the Workbench leaves the pilot (Q-3). | I · **E** (Q-3) |

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
| RT-1 | **The transport is server-sent events, from async handlers, and every command is a POST.** Two streams: the GM channel, `GET /campaigns/{campaign_id}/stream`, authenticated by the session cookie and ownership (SEC-2); the table channel, `GET /table/stream`, authenticated by the table credential alone (SEC-1), the session coming from the credential. The client reads the stream with `fetch` and a `ReadableStream` — **not the native `EventSource`**, which discards comment lines, exposes neither the response status nor its headers, and gives up silently on a non-200 answer, so the heartbeat (RT-2), a refused stream (RT-8) and TABLE-7's silence *from the last byte* would all be invisible through it. The SSE framing is kept, so the server side is standard and a plain `EventSource` still works for a debugging session. A stream never mutates anything, so it needs no origin check; the POSTs that do are SEC-7's. There is no WebSocket in v1, so SEC-7's upgrade clause has nothing to apply to. | P (AUDIO-24, REVEAL-24) + R + I |
| RT-2 | **A frame is one event of the wire contract's realtime family**: `event:` names the kind, `data:` is one JSON object with `schema_version` and its kind; an audio frame names its slot and the slot's sequence; a GM frame also names the link generation and the audio epoch; a table frame carries neither, nor any id it has no use for (SEC-15), and never more text than the projection allows. The heartbeat is an SSE **comment** line every 5 s (*suggested*, TABLE-7) — not an event, nothing to parse, and it keeps the grace of X-4 running from the last byte. Neither `retry:` nor `id:` is used: reconnect timing is the client's (RT-3) and resumption is by snapshot (RT-4). A stream has a bounded queue of 64 frames (*suggested*); a client that cannot drain it is closed with `reconnect` and takes a fresh snapshot, so a slow phone costs memory only until then. | P (TABLE-7, SEC-15) + I |
| RT-3 | **A stream is bounded in time.** The server closes every stream at a jittered moment between 240 and 280 s (*suggested*, under the platform's 300 s, F-1) with a final `reconnect` frame, so two instances' streams never reconnect as one herd, and on SIGTERM the same way: a handler sets a flag every stream checks at its next heartbeat and closes on, and uvicorn runs with `--timeout-graceful-shutdown 8`, so it never waits on open connections past Cloud Run's ten seconds (F-9). The client reopens at once after a `reconnect`, and with jittered exponential backoff after anything else (AUDIO-15, WT-14). A request slot is therefore never held longer than 280 s, a reconnect is routine rather than an error (TABLE-6), and an instance being replaced is invisible to a table. | R + P (TABLE-6) + I |
| RT-4 | **Snapshot, then events** (AUDIO-15). Every stream begins with the frames its channel's snapshot resource answers with (`GmSnapshot`, `TableSnapshot`): the session, then each slot the recipient is entitled to with its content and sequence — for a table client, the table slot and its own participant slot only (REVEAL-24); for the GM, every slot with the epoch — then **`ready`**, the boundary before which nothing is trusted. The server subscribes the stream to the bus **before** it reads the snapshot, and every frame carries the sequence of the state it was read from, so nothing that commits between the two is lost and nothing older than the snapshot is applied. Events follow with sequences above the snapshot's; the client keeps a high-water mark per slot, drops anything at or below it, and on a gap closes the stream and reopens, which yields a fresh snapshot. Hiding, freezing or reconnecting blanks content and resets the marks (TABLE-7, TABLE-11). Nothing a client held before a reconnect is trusted afterwards. | P (AUDIO-15, TABLE-7, TABLE-11) |
| RT-5 | **Postgres is the bus.** The transaction that changes a slot — reveal, update, replace, clear, Stop, push, End, Rotate, a presence roll-up — does two things atomically: writes the state with `seq = seq + 1 RETURNING seq` under the row's lock, and `NOTIFY` on the session's channel with the session id as the whole payload, which Postgres delivers on commit. **A notification is a wake-up, never the record.** Each instance runs one **listener** task on a dedicated connection outside the pools (F-3); on a wake-up — and every 2 s (*suggested*) regardless, so that a missed notification costs latency and never an event — it reads the **current state** of every session it has a subscriber for in one query on its async pool: each slot's content, sequence and generation, each subscribed credential's validity, the lane and presence rows; then it hands each local stream whatever lies above that stream's per-slot marks and within its entitlement. There is no cursor over outbox ids: ids are assigned at insert and commit out of order, so a cursor that passed an uncommitted row would skip it for good, and a lost Stop is the one thing this design may never lose (X-3). Sequences are assigned in the state row under its lock, so they commit in order and the marks are exact. The outbox therefore carries **jobs** only (deletion, sweeps), claimed with `FOR UPDATE SKIP LOCKED` and retried until done, and a wake-up for a session with no local subscriber is ignored. The outbox table, the job runner and the pools — a sync pool for routes and a small `AsyncConnectionPool` for the realtime path, so a reconnect storm's snapshots never take request-pool threads — are `1kg.1.5`'s; the listener's connection, the channel names, the 2 s poll and the state query are this rule's. | P (AUDIO-24) + R + I |
| RT-6 | **Ordering and idempotency.** A sequence is assigned by the database in the changing transaction, so it is total per slot; across the bus an event may still arrive late or twice, and RT-4's marks make that harmless. A command carries its `command_id` — scoped to the caller (wire contract, Idempotency) — and the epoch it was issued under (REVEAL-22, AUDIO-28): a stale epoch is `409 conflict`, a replayed id returns the first outcome and does nothing, and the server orders commands by the transaction that commits them, never by arrival. A Stop carries no epoch and is never queued (X-3). | P (REVEAL-22, AUDIO-28, X-3) + I |
| RT-7 | **Entitlement is checked per frame, without a database round trip per frame** (SEC-9). The state read of RT-5 carries the link generation; a table stream holds its credential's generation; a frame whose generation differs is not written and the stream is closed. End and Rotate change the generation, so every old-generation table stream on every instance closes within one bus delivery of the transaction, and no frame of the new generation ever reaches a stream opened under the old one (T-5). The GM channel is per campaign, not per generation, and is unaffected by a rotation. Generation is not the only revocation: replacing a device (SEC-11), evicting an idle credential (SEC-10) and removing a participant each revoke one credential while the generation stands, so the revoking transaction also notifies `credential:<id>`, the instance holding that stream closes it with `inactive`, and the 2 s state read re-checks every subscribed credential's validity anyway — a window of at most 2 s, still without a per-frame round trip. A participant slot's frames go only to streams whose credential pair names that participant (SEC-11); a guest's transcript is the same whether or not a private reveal happened (T-8). The GM channel checks ownership once at open and closes when the campaign is deleted or the account loses `dm` (R-2 makes the next command fail either way). | P (SEC-9, SEC-11, REVEAL-24) + I |
| RT-8 | **Connections are bounded and counted where they can be seen.** Each open stream is a row in a `connections` table — session, credential, instance, opened, last seen — refreshed by the stream every 15 s (*suggested*) and considered dead after 45 s; the row is what REVEAL-25's per-session (12) and per-device (3) bounds are checked against on connect, across instances, and what presence is aggregated from (AUDIO-24), so the GM's device count is the truth and not one instance's view. The **service-wide** ceiling is counted in the same table: *suggested* 20 open streams, half of the two instances' forty slots as REVEAL-25 requires, of which 4 are reserved for GM channels so a full table never locks its own GM out. Every bound is checked and the row inserted under one per-session transaction lock (`pg_advisory_xact_lock` on the session), so two instances cannot both admit the last seat. There is deliberately **no per-instance ceiling**: Cloud Run adds an instance when one is about 60 % busy (F-1), so an instance that refused its eleventh stream at ten would refuse it forever while the second instance never started. Beyond the credential bound the join itself answers `full` (SEC-10); beyond a connection bound the stream request is refused with a 503 and a `Retry-After`, the client shows `This table is full` (REVEAL-25) and retries with backoff. At the pilot's settings that is one full table and most of a second (Q-1). | P (REVEAL-25, AUDIO-24, SEC-35) + R + I |
| RT-9 | **Health and degradation are explicit.** `/healthz` gains `realtime: {listener: listening or reconnecting, streams: n, cap: m}` and `media: {store: ok or unavailable}` as fields of their own; a listener that is reconnecting is `degraded` there, and the `status` and `ready` values both Compose health checks assert are untouched by it. Listener down: streams keep working from the 2 s poll (RT-5) with up to 2 s more latency. Object store unavailable: uploads and asset reads answer `503 backend_unavailable` (GM) or fail generically (table); text reveals and audio state are unaffected. Database unavailable: everything fails closed, as today. At the stream cap: `This table is full`. Instance shutdown: `reconnect`, then the client is on another instance. If a stream cannot be opened at all, the table client reads its snapshot resource (`TableSnapshot`) every 5 s (*suggested*) and says so with the `Reconnecting…` banner of TABLE-6; polling is a degraded mode, never the design. Billing detached by the budget kill-switch (MS-11): the whole service stops, mid-session, and comes back only when the owner re-attaches it — which is why no media capability is on until the cap is raised (Q-5). | P (the bead's AC) + I |
| RT-10 | **Proxy and platform settings** (`1kg.9.5`): Cloud Run `--timeout 300` unchanged and no session affinity; `--concurrency` per Q-1. nginx, for the two stream locations: `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 320s`, `proxy_http_version 1.1`, `proxy_set_header Connection ""`; the service also sends `X-Accel-Buffering: no` on every stream. For upload locations: `client_max_body_size` equal to the route's SEC-26 cap, `proxy_request_buffering off`, so bytes stream to the service and MS-5's count is real, and `proxy_read_timeout 180s`, because the request stays open through processing (MS-6). The E2E stack runs streams and uploads through nginx, so a wrong nginx setting fails in CI; production has no nginx, and its settings are Cloud Run's (F-1, R-6, T-17). | R + P (R-6) + I |
| RT-11 | **What realtime costs**: a request slot per open stream for at most 280 s at a time; an instance kept warm while any stream is open (a live table is a live service, which is right); a few bytes of heartbeat every 5 s; one `connections` write every 15 s per stream; one `NOTIFY`, one outbox row and one read per instance per change. No new billable infrastructure. | I |
| RT-12 | **The GM channel is the same mechanism with the GM's entitlements**: lane status for invocations and edits, reveal and audio state with the epoch and every slot, presence with aliases and guest counts (threat model 8.3). Two GM tabs are two streams that converge through the same events (AUDIO-24); a GM without a stream still sees every state on the next read, because the stream carries nothing the resources do not. | P (AUDIO-24, 8.3) |
| RT-13 | **The scale path changes settings, not the protocol.** Past the pilot, streams move to a second Cloud Run service built from the same image — `--concurrency 250`, async routes only, the same `/table/stream` and `/campaigns/{id}/stream` paths behind a URL-map load balancer on the same origin — and the request-serving service keeps its slots for login, chat and tools. Because the bus is the database, nothing in RT-1 to RT-9 changes. Not in v1: a load balancer costs more per month than the pilot's whole budget (Q-1). | I · **E** (Q-1) |
| RT-14 | **The Live Session Assistant rides these streams.** Capture state reaches table clients as booleans only, on the table channel, under RT-4's snapshot rule; the GM's capture controls are POSTs; there is no second channel and no second fanout (`agent-forge-harness-1ir.1.11`). | P (`1ir.1.11`) |
| RT-15 | **Background work is request-driven**, because the platform allocates CPU only while a request is in flight (F-1) and always-on CPU costs more than the pilot's whole budget. The listener runs while any stream is open — a stream is a request. Jobs run from three places and nowhere else: inline, after the transaction that created them (a delete tries the bucket at once, and the outbox row is its retry record); from a hook on ordinary requests, which claims and runs one due job every so often; and from an authenticated `POST /internal/jobs` that Cloud Scheduler calls every ten minutes (*suggested*; the first three jobs are free), so a quiet service still sweeps. Sweeps — stuck uploads (MS-3), expired credentials and codes (SEC-37), orphaned objects (MS-3) — are jobs. Nothing is a background thread that expects to run between requests. | R + I |

## 5. Load-test targets

These are the threat model's T-15, made concrete. `1kg.9.5` chooses the tool and
runs them against a Compose stack of two service replicas behind nginx and
against a Cloud Run staging deploy at the pilot settings. A target that fails
moves a *suggested* number in this document, with the measurement beside it. Every target runs its loads **concurrently**: the streams, the chat requests and the logins of LT-1 at once, LT-3's push while LT-1's load holds.

| # | Target | Proves |
| --- | --- | --- |
| LT-1 | With 20 table streams open and heartbeating across two sessions and 12 phones each fetching a 20 MB cue, 20 concurrent `/chat` requests and 10 logins complete with p95 latency within 1.5× their no-stream baseline, and the 21st stream is refused within 200 ms. | SEC-35, REVEAL-25, RT-8, MS-7 |
| LT-2 | A Stop reaches every entitled stream across both instances within 1 s at p95 and 2.5 s at worst (X-3, AUDIO-13's 3 s). | RT-5, RT-7 |
| LT-3 | A push is audible on 12 players of one session within 3 s at p95, measured from the POST to the first byte of media fetched (AUDIO-13). | RT-5, MS-7 |
| LT-4 | 20 streams reconnecting at once at the 280 s boundary complete their snapshots within 2 s at p95, and chat p95 stays within LT-1's bound throughout. | RT-3, RT-4 |
| LT-5 | A 10 MB image and a 20 MB ambience each process within 15 s at p95 with one job at a time; twenty concurrent uploads are each accepted, queued or told to retry with `Retry-After`, none is lost, and instance memory stays under 900 MiB throughout. | MS-6, RT-15 |

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
- **Realtime frames** carry `schema_version` and their kind; an audio frame its
  slot and the slot's sequence; a GM frame the link generation and the audio
  epoch as well; a table frame neither. Kinds today — GM: `tool_lane`,
  `edit_lane`, `session`, `audio`, `presence`, `asset`, `ready`, `reconnect`;
  table: `session`, `inactive`, `audio`, `ready`, `reconnect`. `ready` marks the
  end of a snapshot, the boundary TABLE-7 needs; `slot` arrives with the reveal
  family. The heartbeat is a comment, not a frame. A table frame carries only
  what the projection of its slot allows.
- **Snapshot resources** `GmSnapshot` and `TableSnapshot`: the frames a stream
  sends first, ending with `ready`, readable on their own for the polling mode
  (RT-9). `TableSession` carries the audio epoch, so two GM tabs converge
  (AUDIO-24).
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
| Q-4 | A per-campaign monthly egress budget, and media pausing for that campaign when it is spent (MS-11)? | **Yes**, at 3 GB while the cap is $10, until `yje` sets real numbers. | Without it one leaked link and a script can turn a 20 MB ambience into the month's bill — and under the current cap, into a detached billing account. |
| Q-5 | Raise the $10 monthly cap before any media capability is switched on? | **Every media capability stays off until the cap covers it.** The number is the owner's; a bucket, egress and the larger image cost cents to a few dollars a month at the quotas above, on top of Cloud SQL's $9.40. | The kill-switch detaches billing at 100 % (MS-11); with sixty cents of headroom, the first busy session would end the month. |

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
- SEC-27's *one fixed format* is read here as one format **per family** for
  images (PNG, JPEG and WebP each re-encoded to itself, so alpha survives) and
  one format for audio (MS-6); the threat model's next amendment should say so.

### 8.3 Beads this refines

| Bead | What it must now do |
| --- | --- |
| `1kg.1.5` | Two bounded pools with clean shutdown — sync for routes, an `AsyncConnectionPool` for the realtime path (F-3, RT-5); the outbox table for jobs only, and a job runner using `FOR UPDATE SKIP LOCKED` that runs inline, from a request hook and from `/internal/jobs` (RT-15); the `connections` table; one dedicated listener connection outside the pools (RT-5, RT-8) |
| `1kg.1.2` | The media and realtime families of section 6 |
| `1kg.2.3` | Join answers the bounds (RT-8); `connections` rows are how presence and device counts are read |
| `1kg.7.5` | RT-1 to RT-9 for the table channel: the `fetch`-based stream reader with jittered backoff, shared with the GM channel; snapshot-then-events with `ready`, per-slot marks, the jittered close, the generation and credential checks, the snapshot resource as the degraded mode |
| `1kg.8.1` | MS-1 to MS-11: the bucket, the store protocol and its three implementations, the streaming upload, the processing subprocess, the asset routes, import behind its flag |
| `1kg.8.2` | Derivatives from the processed original only, states of MS-3 |
| `1kg.8.6` | Audio slots on the bus: `started_at` on the server's clock, sequence and epoch per RT-5 and RT-6 |
| `1kg.9.2` | The `/healthz` fields and the stream, listener, upload and egress metrics with closed labels (RT-9, SEC-22) |
| `1kg.9.5` | The bucket and its IAM binding, ffmpeg and Pillow in both images, nginx locations and settings (RT-10), Cloud Run flags (Q-1, `--timeout-graceful-shutdown 8`, RT-3), the Cloud Scheduler job behind `/internal/jobs` (RT-15), LT-1 to LT-5, and the runbooks: bucket loss, orphan reconciliation, listener health, quota and egress overrides, a detached billing account (MS-11) |
| `1kg.9.6` | Q-3 and Q-1 as rollout gates |
| `agent-forge-harness-1ir.1.11` | Rides RT-1 and RT-14; capture state is booleans on the table channel |
| `agent-forge-harness-yje` | The quota and egress numbers of MS-4 and MS-11 |

## 9. Review log

Each review, its findings and what was done about them, as the interaction
record's section 18 does.

### 9.1 Independent adversarial review (2026-09-17)

A reviewer with no part in writing this document read it against the threat
model, the interaction record, the wire contract and the code, and verified the
facts of section 2 in the worktree. One Blocker, eight High, fifteen Medium,
eight Low. The reviewer numbered its findings with an F; they are RV-n here, because F-n names this document's own facts. Dispositions:

| # | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| RV-1 | Blocker | `GET /assets/{asset_id}` would shadow the built UI's bundle path, which the SPA mount serves from `/assets/` | **Fixed**: the route is under the campaign, and no API route may begin with `/assets/` (MS-7) |
| RV-2 | High | The native `EventSource` cannot see comment lines, statuses or headers, so the heartbeat, a refused stream and TABLE-7's grace were unobservable | **Fixed**: the client reads the stream with `fetch` and a `ReadableStream`; SSE framing kept (RT-1, RT-2) |
| RV-3 | High | A cursor over outbox row ids skips a row that commits out of id order — a lost Stop | **Fixed**: a notification is a wake-up only; delivery reads current state per session; the outbox carries jobs only (RT-5) |
| RV-4 | High | A per-instance ceiling of ten sits below the autoscaler's 60 % point, so the second instance would never start | **Fixed**: a service-wide ceiling counted in `connections`, no per-instance cap; the autoscaler rule recorded in F-1 (RT-8) |
| RV-5 | High | Background work assumed CPU that request-based Cloud Run does not allocate | **Fixed**: RT-15 — jobs run inline, from a request hook and from a Scheduler-called endpoint; the listener rides open streams |
| RV-6 | High | The $10 hard cap and its billing kill-switch, with about sixty cents of headroom, were not in the cost model | **Fixed**: MS-11 states the numbers; media capabilities stay off until the cap is raised (Q-5); egress default 3 GB (Q-4); the detached state in RT-9 |
| RV-7 | High | A single revoked credential kept its stream until the 280 s close, because only the generation was checked | **Fixed**: a `credential` wake-up closes the stream, and the 2 s state read re-checks every subscribed credential (RT-7) |
| RV-8 | High | Table media reads were uncounted request slots | **Fixed**: at most six byte responses per instance, 503 beyond; LT-1 runs with media in flight (MS-7) |
| RV-9 | High | `google-cloud-storage` is synchronous and would block the loop or take request-pool threads | **Fixed**: a dedicated thread limiter of six, doubling as the media ceiling (MS-10) |
| RV-10 | Medium | Section 6 promised kinds the contract did not carry, no snapshot boundary, no snapshot resource for polling | **Fixed**: `ready` on both channels, `GmSnapshot` and `TableSnapshot` in the contract; §6 rewritten; `slot` stays deferred |
| RV-11 | Medium | The audio epoch never reached the GM channel, so two tabs could not converge | **Fixed**: on the GM `audio` frame and on `TableSession` (contract) |
| RV-12 | Medium | `Image.MAX_IMAGE_PIXELS` at the cap only warns | **Fixed**: half the cap (MS-6) |
| RV-13 | Medium | The no-video-stream probe would reject cover art | **Fixed**: attached pictures exempt (MS-6) |
| RV-14 | Medium | "on SIGTERM the same way" had no mechanism | **Fixed**: a flag checked at the heartbeat and `--timeout-graceful-shutdown 8` (RT-3) |
| RV-15 | Medium | A fixed `retry:` and synchronized 280 s closes contradict AUDIO-15's backoff | **Fixed**: jittered close, client-owned backoff, no `retry:` (RT-2, RT-3) |
| RV-16 | Medium | Subscribe-then-snapshot order and frame sequences were unspecified | **Fixed** (RT-4) |
| RV-17 | Medium | Whether the byte upload waits for processing was undecided; LT-5 tested two uploads where T-15 says twenty | **Fixed**: the request stays open, queueing bounded at 60 s, 503 with `Retry-After` beyond; the `asset` frame; LT-5 at twenty (MS-6, RT-10) |
| RV-18 | Medium | No timeout for stuck `uploading`/`processing` rows or their reservations | **Fixed**: a sweep job (MS-3, RT-15) |
| RV-19 | Medium | No per-credential stream bound, and a count-then-insert race across instances | **Fixed**: a per-session transaction lock; the per-device bound (RT-8) |
| RV-20 | Medium | No backpressure for a slow client | **Fixed**: a bounded queue; overflow closes the stream (RT-2) |
| RV-21 | Medium | E2E has no Postgres, the bus had no fake, and `degraded` must not change the health `status` | **Fixed**: an `InMemoryBus` for unit tests and E2E; health fields of their own (MS-10, RT-9) |
| RV-22 | Medium | "The pool keeps serving chat" needs an async database path | **Fixed**: a small `AsyncConnectionPool` for the realtime path, sized with the sync pool under the instance's ceiling (F-3, RT-5, 8.3) |
| RV-23 | Medium | MS-1 and MS-12 cited SEC-36 for "deleted means deleted", which SEC-36 qualifies with a backup window | **Fixed**: the wording says why media has no such window (MS-12) |
| RV-24 | Medium | Three citations did not contain the facts they were cited for | **Fixed**: F-9, F-11 and MS-10 re-cited |
| RV-25 | Low | `gen` on table frames is an identifier the client does not need | **Fixed**: removed from the contract's table frames; GM frames keep it (RT-2) |
| RV-26 | Low | RT-7 read literally would close the GM's own stream on Rotate | **Fixed**: the generation check is the table channel's (RT-7) |
| RV-27 | Low | MS-7 omitted SEC-19's CSP header; MS-6 reinterpreted SEC-27's fixed format without saying so | **Fixed** (MS-7, 8.2) |
| RV-28 | Low | ffmpeg's MP3 muxer still writes an encoder tag | **Fixed**: `-id3v2_version 0 -write_id3v1 0` (MS-6) |
| RV-29 | Low | RV-3 never stated the connection ceiling | **Fixed**: 25, 22 usable; the pools sized to it |
| RV-30 | Low | Outbox rows for unsubscribed sessions and cursor initialisation were unstated | **Moot**: there are no event rows and no cursors (RT-5) |
| RV-31 | Low | "fails in CI and not in a session" was a non sequitur | **Fixed** (RT-10) |
| RV-32 | Low | The load tests did not say which loads run concurrently | **Fixed** (section 5) |

Raised and not confirmed by the reviewer, recorded so nobody re-derives them:
how Chrome's media element seeks and loops with `no-store` and no validator; whether
`RLIMIT_AS` is enforced in Cloud Run's first-generation sandbox; whether the
HTTP/2 front end forwards `Content-Length` for browser uploads (MS-4's 411);
Starlette's `BaseHTTPMiddleware` with long streams and disconnects; an idle
`LISTEN` connection through the Cloud SQL proxy under CPU throttling (RT-9's
reconnect covers it). Each is a thing for `1kg.9.5`'s load tests and `1kg.8.1`'s
first prototype to measure, not a rule to write.
