# Runbook: provider usage capture, and turning it into a number

**Bead:** `agent-forge-harness-yje.5.1.1` ("yje.5.1 slice a — measure now").
**What this gives you:** the real cost of a chat turn, by mode and by account, from
production. Until this shipped, the service measured **no model cost at all**, so every
figure in `docs/forge/plans/subscription-billing-coupons-profitability.md` was a
list-price estimate rather than a measurement.

**What this is not:** a table, a migration, a price table in code, or a dashboard.
Slice b (`agent-forge-harness-yje.5.1.2`) replaces the sink behind these field names
with a durable ledger; `yje.5.4a` adds p50/p95 per account, starting from these same
logs. Nothing here needs a schema change to get there.

---

## 1. What one record means

One record is **one request sent (or attempted) to a provider over the network** — success
or failure, including every retried attempt and the final failure. It is written by
`service/usage_capture.py` as one JSON line on stdout, which Cloud Run parses into
`jsonPayload`.

Five capture points produce them, all inside a live `POST /chat` turn:

| `purpose` | What it paid for |
| --- | --- |
| `embedding` | The query embedding (`text-embedding-3-small`) |
| `answer` | The grounded answer |
| `suggestions` | The three spell-usage ideas (spell mode) |
| `spell_structuring` | The structured spell card (spell mode) |
| `statblock_structuring` | The structured NPC/stat block (sage/GM, behind the cost heuristic) |

There is **no one-per-turn record**. The shared `operation_id` is what groups a turn's
attempts — count distinct `operation_id` values if you want turns.

### The fields

| Field | Type | Meaning |
| --- | --- | --- |
| `event` | str | Always `provider_attempt`. **This is the filter key** — filter on it, never on message text. |
| `record_version` | int | `1`. Bumped if the shape changes. |
| `operation_id` | str | uuid4 hex, one per `/chat` turn, shared by every attempt of that turn. |
| `operation` | str | `chat_turn` (slice b adds more). |
| `purpose` | str | One of the five above. |
| `mode` | str | `sage` \| `spell` \| `rules` \| `gm`. |
| `alias` | str | The alias **actually handed to the provider client** — not the disclosure label the model picker shows the user. |
| `provider` | str \| null | From the model catalog, or `openai` for embeddings. **Null when unknown — never guessed.** |
| `retry_index` | int | `0` for the first attempt at a call site, `+1` per service-owned retry. |
| `status` | str | `ok` when the provider returned a response, `error` when the call raised. |
| `error_class` | str \| null | `type(exc).__name__` **only** — never the exception message, never a traceback. |
| `finish_reason` | str \| null | As the provider reported it; null for embeddings and errors. |
| `input_tokens` | int \| null | As the provider reported it. |
| `cached_input_tokens` | int \| null | The cached subset of `input_tokens`, as reported. |
| `output_tokens` | int \| null | As reported. |
| `reasoning_tokens` | int \| null | As reported. |
| `latency_ms` | float | Wall time around the provider call **only**. Excludes retry backoff sleeps. |
| `provider_request_id` | str \| null | See the honest-limits section — this is the chat-completion id, not OpenAI's `x-request-id`. |
| `billed_account_id` | int | The account that pays, as a raw internal integer. |
| `actor_kind` | str | `account` \| `participant` \| `guest` \| `system`. `account` today. |
| `campaign_id` | str \| null | Null today. The key exists so slice b needs no migration to add it. |

The key set is **closed and asserted in tests**: a record has exactly these keys, in every
branch. Cloud Run's structured logging then adds `severity` and `message`, and
`logging.googleapis.com/trace` when the request carried a valid trace id.

**No prompt, answer, suggestion, spell or stat-block text, attachment name or text, source
text, email address, session token, API key, exception message or stack trace can appear
in a record.** That is what the closed key set is for, and a test drives a turn with unique
marker strings and greps every record for them.

`billed_account_id` is an internal integer account id. It is deliberate, and allowed here
because this is an access-controlled structured log — `service/app.py` already logs
`user_id` the same way. It must **never** become a metric label
(`docs/observability/metrics-standard.md:59-61`).

### One real record

Emitted by the real emitter (a spell-mode answer), reformatted onto one line:

```json
{"severity": "INFO", "message": "provider attempt", "event": "provider_attempt",
 "record_version": 1, "operation_id": "4f1d2c7a9b6e4d1f8a3c5e7b9d0f2a41",
 "operation": "chat_turn", "purpose": "answer", "mode": "spell", "alias": "gpt-4o-mini",
 "provider": "openai", "retry_index": 0, "status": "ok", "error_class": null,
 "finish_reason": "stop", "input_tokens": 6012, "cached_input_tokens": 0,
 "output_tokens": 588, "reasoning_tokens": null, "latency_ms": 41.7,
 "provider_request_id": "chatcmpl-BkQ2yV8sample", "billed_account_id": 1,
 "actor_kind": "account", "campaign_id": null,
 "logging.googleapis.com/trace": "0af7651916cd43dd8448eb211c80319c"}
```

---

## 2. Reading the records out of Cloud Logging

Logs Explorer filter, ready to paste:

```text
resource.type="cloud_run_revision"
AND resource.labels.service_name="<service>"
AND jsonPayload.event="provider_attempt"
```

The equivalent `gcloud` command, which is what the report reads:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="<service>"
   AND jsonPayload.event="provider_attempt"' \
  --project <project> \
  --freshness 30d \
  --format=json > attempts.json
```

`--freshness` must stay inside your log bucket's retention window — see the honest-limits
section, which gives you the command that tells you what that window is.

---

## 3. Turning the records into cost

```bash
uv run --frozen --no-sync python scripts/usage_cost_report.py attempts.json \
  --input-price 0.15 \
  --cached-input-price 0.15 \
  --output-price 0.60 \
  --embed-price <owner supplies>
```

It prints **cost by mode per day**, **total cost per account per month**, and **the count of
attempts whose token counts the provider never reported**. `--json` prints the same report
as JSON. It reads a `gcloud`-style JSON array or one JSON object per line, and `-` reads
stdin.

**No percentiles.** p50/p95 per account belongs to `agent-forge-harness-yje.5.4a`, which
starts from these same logs; two beads must not own the same numbers.

**Every price is a required argument with no default.** Running it without one exits
non-zero and prints nothing but the usage error. That is deliberate: there is no price
table in code (that is slice b's), and no price may silently default to zero.

### Where the day comes from

A record carries **no timestamp of its own** — Cloud Run's structured logging owns the
`timestamp` field name, so a record may not use it. The day and month therefore come from
the log entry envelope. Records handed to the tool without an envelope timestamp are
reported as `undated_records` and excluded from the per-day and per-month tables rather
than being bucketed somewhere convenient.

### Worked example

Five real records from one spell-mode turn (embedding, answer, suggestions,
spell structuring, plus one failed structuring attempt with no reported tokens), at the
price table below with a placeholder `--embed-price 0.02`:

```text
Cost by mode per day (USD, list prices supplied on the command line)
  day          mode                 cost
  2026-09-21   spell          0.00167579

Total cost per account per month (USD)
  month        account              cost
  2026-09      1              0.00167579

Provider attempts read: 5
Attempts with unreported (null) token counts: 1 (counted, never priced as zero - the bill for these is not visible here)
Records with no log-entry timestamp: 0 (excluded from the per-day and per-month tables)
Total priced cost: 0.00167579 USD
```

Checked by hand:

```text
embedding            7 in   @ $0.02/1M  = $0.00000014
answer            6012 in   @ $0.15/1M  = $0.00090180
                   588 out  @ $0.60/1M  = $0.00035280
suggestions        812 in   @ $0.15/1M  = $0.00012180
                   121 out  @ $0.60/1M  = $0.00007260
spell_structuring  655 in   @ $0.15/1M  = $0.00009825
                   214 out  @ $0.60/1M  = $0.00012840
statblock (failed) null             --- = $0          (counted as 1 null-token attempt)
                                   total = $0.00167579
```

---

## 4. The price table

Paste these into the command above. **Every price carries its source and the date I read
it.** Nothing here was guessed, and nothing defaults to zero.

| What | Price (USD per 1M tokens) | Source, and the date read |
| --- | --- | --- |
| `gpt-4o-mini` input | **$0.15** | `docs/forge/plans/subscription-billing-coupons-profitability.md:328-334`, which itself cites the OpenAI pricing page at `:617-630`. Read 2026-09-21. |
| `gpt-4o-mini` output | **$0.60** | Same source, same date. |
| `gpt-4o-mini` cached input | **$0.15 — priced at the full input rate** | Not in the billing plan, and not confirmed against the OpenAI pricing page. Priced at the full input rate as a deliberate **over-estimate**: it cannot understate the bill. Replace it with the real cached rate when you have it, and the number will only go down. |
| `text-embedding-3-small` | **owner supplies — unpriced until then** | Not in the billing plan. `--embed-price` is a required argument with **no default** and is never zero. Until you supply it, the monthly figure is incomplete. |

The two missing prices are on the OpenAI pricing page the plan already cites
(`https://developers.openai.com/api/docs/pricing`). They are not written in here as
numbers because I did not read that page — writing down a price I did not read is exactly
the kind of claim this runbook exists not to make.

---

## 5. Honest limits — read this before quoting any number from it

**Nothing is recorded unless the service actually deploys.** `.github/workflows/ci.yml`
gates its `deploy` job on `github.event_name != 'pull_request'`,
`github.ref == 'refs/heads/master'` and the repository variable `vars.DEPLOY_TARGET != ''`
(`:236-248`), and the auth/deploy steps are further gated on the WIF secrets
(`GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`, `:254-270`). **Repository variables and secrets are
not visible from a checkout**, so this cannot be verified from the code. The owner must
confirm `DEPLOY_TARGET` and the WIF secrets are configured — otherwise this records zero
rows forever while every check stays green.

**Records start at deploy. There is no backfill.** The data was never recorded, and no
amount of work makes it exist retroactively.

**Emission is synchronous by design, so a slow log sink adds its own latency to the turn.**
Each record is written inline on the request path (`print(json.dumps(...), flush=True)` to
stdout) because this slice forbids batching, a queue or a background thread; a sink that
blocks — a stalled stdout pipe on Cloud Run is the only realistic case — slows the turn by
exactly that much. A sink that *fails* is harmless (it is caught and dropped); a sink that
*hangs* is not. If that ever happens the fix is an asynchronous sink, not a wider guard.

**Retention is whatever your project set, and this runbook does not know it.** The records
land in the `_Default` Cloud Logging bucket. Google's default retention for `_Default` is
30 days, but the project's actual value cannot be read from a checkout. Ask it:

```bash
gcloud logging buckets describe _Default \
  --location=global --project=<project> \
  --format="value(retentionDays)"
```

Run the report inside that window, or export the logs before it closes. (Export, sinks and
log-based metrics are an infra decision, not this bead's.)

**"One record per attempt" means per `embed_query` call, not per HTTP request.** The OpenAI
SDK retries embedding requests internally up to 2 times by default
(`openai/_constants.py: DEFAULT_MAX_RETRIES = 2`), and those retries are invisible here.
Generation retries are the service's own and **are** recorded individually, with
`retry_index` counting up.

**A turn that fails before a request is sent records nothing.** A missing `OPENAI_API_KEY`
raises before the embedding request leaves the process, so it is not an attempt. A turn
refused by a gate (rate limit, daily cap, role, ownership, model preference) makes no
provider call at all.

**`provider_request_id` is the chat-completion id, not OpenAI's `x-request-id`.** It comes
from the response object's `id`. OpenAI's dashboard and support use `x-request-id`, which
the LangChain wrapper does not surface here. Useful for correlating our own records; not a
support handle.

**Provider-reported tokens are the only source.** A provider that reports nothing produces
nulls, and the report counts those attempts separately and prices them at nothing. That
count is the size of the bill this report cannot see — if it is large, the totals are a
floor, not an estimate.

**A failed attempt usually has null tokens**, so a period with many provider errors will
show a larger null count. Whether the provider billed for those attempts is not something
these records can answer.

**The pilot population is small**, so any per-account figure is noisy. A single heavy user
moves the mean.

**The invoice remains the truth check.** Reconciling these totals against a sampled OpenAI
invoice is the owner's step in `agent-forge-harness-yje.1.2`; this runbook tells you how to
produce the number, it does not automate the reconciliation. The parent bead
`yje.5.1`'s "sampled invoices reconciled within the approved tolerance" is that bead's
criterion, not this slice's — no tolerance exists yet because `yje.1.2` is unapproved.

---

## 6. If the report comes back empty

In order, cheapest first:

1. Did it deploy? Check that `DEPLOY_TARGET` and the WIF secrets are set (section 5) and
   that the `deploy` job actually ran on the merge commit.
2. Is the filter's `service_name` right? It is the Cloud Run service name, not the repo.
3. Is `--freshness` inside the bucket's retention window (section 5)?
4. Has anyone used `/chat` since the deploy? Records only exist for live turns — an
   eval script, a local run or the E2E app produces none, by design.
