# Cost and profitability model: Live Session Assistant

Date: 2026-09-16
Status: planning model — inputs are list prices and stated assumptions, not measured production usage
Beads: epic `agent-forge-harness-1ir`; `LSA-x.y` is `agent-forge-harness-1ir.x.y`
Related plans:

- `docs/forge/plans/live-session-assistant.md` — architecture
- `docs/forge/plans/subscription-billing-coupons-profitability.md` — `yje`; owns pricing, fees, and COGS guardrails
- `docs/forge/plans/live-session-assistant-delivery.md` — cost beads `LSA-1.9` and `LSA-8.*`

> Every price below was read from a first-party pricing page on 2026-09-16
> (sources in §17). Prices, promotions, and model lifecycles change often.
> Values must be stored as effective-dated price revisions in the cost ledger
> (`yje.5.1`) and revalidated before enabling any capability.
>
> One-off legal and security review figures are **placeholders for
> procurement quotes**, not estimates of actual fees. This is engineering
> planning, not financial or accounting advice.

## 1. Summary

| Metric | Low | Expected | Stress |
| --- | --- | --- | --- |
| Cost per **listening hour** (timed/session listening, utterance uploads) | $0.12 | **$0.32** | $1.22 |
| Cost per **push-to-talk clip** | $0.0010 | **$0.0022** | $0.0221 |
| Cost per **transcript-upload hour** (text only) | $0.04 | **$0.08** | $0.74 |
| Heavy GM using the full $25 plan allowance, monthly live COGS (12 h + 150 clips) | $1.54 | **$4.19** | $18.00 |
| Weekly 4-hour GM on the $29 plan, monthly live COGS (17.3 h + 150 clips) | $2.16 | **$5.90** | $24.48 |

**Conclusions:**

1. **Push-to-talk belongs in the base plan.**
   - A typical base GM (100 clips, 1 trial listening hour, 2 transcript-upload
     hours) adds about **$0.70/month** of COGS.
   - Total COGS with chat is **$2.20**, inside the billing plan's **$3 hard
     cap**, for a **79.2%** contribution margin at $15.
2. **Continuous listening belongs in a premium tier with a capped allowance.**
   Every persona below is capped at its plan's allowance, and each figure is
   the expected contribution margin.
   - **$25 with 12 hours:**
     - a heavy GM using the whole allowance (total COGS $5.69): **71.9%**;
     - a power GM at the allowance (400 clips, 4 transcript hours): 68.5%,
       under the 70% target.
   - **$29 with 20 hours:**
     - a weekly 4-hour table (17.3 h): 69.4%, just under the target; at $30 it
       reaches 70.2%;
     - a power GM at 20 h: 63.4%.
   - **$19 with 12 hours:**
     - a heavy GM: 64.4%;
     - only +$0.35 of incremental contribution per upgrade over base.
   - **Recommendation for `LSA-1.9`:** launch hypothesis **$25 with 12 hours**.
     - Until Phase 5 there is no session-length listening, only 5–30-minute
       windows, so 12 hours is 24 or more half-hour windows.
     - A weekly 4-hour table that wants whole-session capture needs about
       17 hours. That is the case for a 20-hour tier at about $30 once session
       listening ships.
   - All figures include the pre-storage classifier (§5) that Phase 3 redaction
     needs.
3. **Speech-to-text dominates the expected case, about 60% of a listening
   hour. Model choice dominates the stress case.**
   - Moving extraction and card writing to mid-tier models multiplies AI cost
     roughly 8×.
   - Small, validated extraction models plus deterministic templates are a
     cost control, not just a quality choice.
   - Extraction is debounced to at most one call per 30 seconds, matching the
     architecture. Calling the model on every utterance (≈300 calls per hour)
     would raise the expected listening hour from $0.32 to about $0.38.
4. **Avoid long-lived capture connections.**
   - Cloud Run bills an instance for the full life of any open WebSocket, and
     caps requests at 60 minutes.
   - VAD-gated utterance uploads are billed per request and never send silence.
   - Cheaper wall-clock streaming vendors (Soniox $0.12/h, AssemblyAI $0.15/h)
     only win once connection costs are shared at scale. That is Phase 5
     (`LSA-12.6`).
5. **Gate costs dominate break-even, not unit costs.**
   - Amortizing a *placeholder* $25,000 of one-off legal and security review
     over 12 months needs about **298 premium upgrades** at $25.
   - Validate demand with the push-to-talk MVP before funding the Phase 4
     external review.
6. **No production enablement before the billing kill switch is replaced.**
   - Production runs under a $10 GCP budget whose kill switch detaches billing
     from the whole project, and the database alone uses about $9.40 of it.
   - Phase 1 adds small recurring items (KMS keys, scheduled retention jobs,
     per-role secrets), and later phases add about $64/month of fanout and
     database costs. Either could trip the switch.
   - `yje.6.6` therefore blocks the Phase 1 gate (`LSA-13.2`) and `LSA-9.3`
     (§13.4).
7. **The market prices post-session tools at about $0.72–$1.10 per audio hour,
   or $4–$60 per month** (§10.3). Those tools transcribe after the session; none
   was found offering live, permission-aware cards. The expected live cost of
   $0.32 per listening hour leaves room under those anchors, but plan
   allowances must be compared against them in `LSA-1.9`.

---

## 2. Units and definitions

| Unit | Definition |
| --- | --- |
| **Listening hour** | One wall-clock hour of an active timed or session capture window (paused time excluded). An internal cost unit only: user-facing terms come from `LSA-1.8`, because the Workbench uses "listening" for audio playback. |
| **Speech fraction** | Share of a listening hour containing speech after client VAD. 0.60 / 0.675 / 0.75 (low/expected/stress), from conversational speaking-rate research: Yuan, Liberman & Cieri 2006; NCVS |
| **Billed audio minutes** | Speech minutes × VAD padding (1.05 / 1.08 / 1.12) for pre/post-roll and short segments |
| **Clip** | One push-to-talk capture: 8 / 12 / 25 s of speech |
| **Transcript tokens per hour** | 7K–13K tokens for a 4–6 player table (≈1.33 tokens/word; 150–214 wpm; 60–75% speech) |
| **Fees** | 4.1% + $0.30 per charge (Stripe cards 2.9% + $0.30, Billing 0.7%, Tax 0.5%) — the billing plan's conservative assumption |
| **Base chat COGS** | $1.00 / $1.50 / $3.00 per subscriber per month (billing plan target $1.50, hard cap $3) |

---

## 3. Unit-cost inputs

### 3.1 Speech-to-text (list prices per audio hour)

| Vendor / model | Batch | Streaming | Billing basis (streaming) | Diarization | Vocabulary/keyterms | Data-handling notes |
| --- | --- | --- | --- | --- | --- | --- |
| OpenAI `gpt-transcribe` | **$0.27** | $0.27 (transcription sessions) | per audio minute | not documented | keyword hints | Transcriptions endpoint: no abuse-monitoring retention; ZDR-eligible; no training by default |
| OpenAI `gpt-live-transcribe` | — | $1.02 | per audio minute | no | keywords | Realtime endpoint: 30-day abuse monitoring |
| OpenAI `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` / `whisper-1` | $0.36 / $0.18 | — | — | diarize variant | prompt | **Shut down 2027-02-26** — do not adopt |
| Deepgram Nova-3 (mono) | $0.258 (+$0.078 keyterm) | $0.462 regular (promo $0.288) | [unverified] audio duration | +$0.12/h | keyterm ≤500 tokens | **Retains/trains by default** unless `mip_opt_out=true` |
| AssemblyAI Universal-3.5 Pro | $0.21 (+$0.05 keyterms) | $0.45 | session duration | +$0.02/h batch, +$0.12/h streaming | ≤100 keyterms | Streaming ZDR for opted-out customers; pre-recorded audio deleted 24–48 h |
| AssemblyAI Universal-Streaming | — | **$0.15** | wall-clock session time | +$0.12/h | keyterms | Session auto-closes at 3 h |
| Soniox v5 | **$0.10** (async) | **$0.12** | full stream duration | included (≤15) | context ≤8K tokens | Never used to improve models; realtime not stored |
| ElevenLabs Scribe v2 | $0.22 | $0.39 | per audio minute | batch only | ≤50 × 20 chars (realtime) | Zero retention Enterprise-only; history on by default |
| AWS Transcribe | $0.36 | $0.60 | 1-second increments | included | included | **Stores and uses voice inputs by default** unless an org opt-out policy is set |
| Azure Speech | $0.18 batch; MAI-Transcribe-2 promo $0.10 | $1.00 (+$0.30/h diarization) | audio sent | batch included | phrase list free | Real-time/fast: not retained |
| Google Gemini 3.5 Transcribe | ≈$0.30 | ≈$0.54 (Live; 10-min sessions) | tokens | files only (≤8) | ≤1,000 terms; incompatible with diarization | Paid tier not used to improve products |

Planning scenarios:

- **Low:** Soniox async, $0.10/h.
- **Expected:** OpenAI `gpt-transcribe`, $0.0045/min.
- **Stress:** Deepgram Nova-3 batch with keyterms, $0.336/h.

The final vendor is decision `LSA-1.5`, informed by the proper-noun benchmark
(`LSA-7.2`) and legal review of vendor terms (`LSA-1.4.1`). Vendors that retain
or train by default are eligible only with a contractual opt-out.

### 3.2 LLM tokens (per 1M tokens, standard tier)

| Model | Input | Cached input | Output | Lifecycle / notes |
| --- | --- | --- | --- | --- |
| OpenAI `gpt-4o-mini` (current production) | $0.15 | $0.075 | $0.60 | No shutdown listed; strict structured outputs |
| OpenAI `gpt-5.4-nano` | $0.20 | $0.02 | $1.25 | Current; reasoning effort default none |
| OpenAI `gpt-5.6-luna` | $0.20 | $0.02 (writes $0.25) | $1.20 | Current; default reasoning effort **medium** — pin `none` |
| OpenAI `gpt-5.4-mini` | $0.75 | $0.075 | $4.50 | Current |
| OpenAI `gpt-5.6-terra` | $2.00 | $0.20 (writes $2.50) | $12.00 | Current; mid-tier stress proxy |
| OpenAI `gpt-4.1-nano` / `gpt-5-nano` | $0.10 / $0.05 | — | $0.40 / $0.40 | **Retiring 2026-10-23 / 2026-12-11** — do not adopt |
| Anthropic Claude Haiku 4.5 | $1.00 | $0.10 (5-min write $1.25) | $5.00 | Min cacheable prompt 4,096 tokens |
| Anthropic Claude Sonnet 5 | $2.00 | $0.20 | $10.00 | Newer tokenizer ≈ +30% tokens (inferred for Sonnet 5) |
| Google Gemini 2.5 Flash-Lite | $0.10 | $0.01 | $0.40 | ZDR requires Vertex AI; Developer API abuse logs retained 55 days |
| Google Gemini 3.6–3.8 Flash | $0.75 → **$1.50 on 2027-01-01** | — | $3.75 → $7.50 | Promotional pricing ends 2026-12-31 |
| OpenAI `text-embedding-3-small` | $0.02 | — | — | Negligible for query embeddings |

### 3.3 Google Cloud (us-central1, on-demand)

| Item | Price | Relevance |
| --- | --- | --- |
| Cloud Run request-based | $0.000024/vCPU-s + $0.0000025/GiB-s active; $0.40/M requests | Clip and utterance uploads |
| Cloud Run instance-based | $0.000018/vCPU-s + $0.000002/GiB-s | Any open WebSocket is billed this way for its whole duration |
| One 1 vCPU/1 GiB instance-hour | $0.072 (instance) – $0.095 (request-based active) | Realtime connection cost ceiling at low scale |
| Cloud Run max request timeout | 60 minutes (current deploy uses 300 s) | Long capture streams must reconnect |
| Cloud SQL db-f1-micro → db-custom-1-3840 | $7.67 → $49.31/month; SSD $0.17/GiB-month | Production tier likely needed for RLS, outbox, and more tables (shared with Workbench) |
| Memorystore Valkey custom-pico | $22.48/month | Cross-instance fanout (Phase 2+), if chosen over Postgres `LISTEN/NOTIFY` or Pub/Sub |
| Pub/Sub | 10 GiB free, then $40/TiB | Alternative fanout; negligible volume |
| Cloud Storage Standard | $0.020/GiB-month; egress $0.12/GiB (North America) | Temporary uploads; portrait derivatives |
| Cloud CDN | $0.08/GiB egress + $18.25/month forwarding rule | Only for public portrait derivatives; not recommended initially |
| Cloud KMS | $0.06/key version/month; $0.03/10K operations | Envelope encryption (one KEK; wrapped per-campaign DEKs in the database) |
| Cloud Logging | $0.50/GiB after 50 GiB/project/month | Content-free logs are tiny |
| Cloud Scheduler / Tasks | $0.10/job/month (3 free); Tasks 1M ops free | Retention jobs |

---

## 4. Transcription cost per session hour

**Utterance uploads** (recommended for Phases 1–3) bill only speech minutes:

| Scenario | Billed audio minutes / listening hour | STT cost / listening hour | 4-hour session |
| --- | --- | --- | --- |
| Low (Soniox async) | 37.8 | $0.063 | $0.25 |
| Expected (OpenAI `gpt-transcribe`) | 43.7 | $0.197 | $0.79 |
| Stress (Deepgram batch + keyterms) | 50.4 | $0.282 | $1.13 |

**Streaming** bills wall-clock time, silence included (Phase 5 option):

| Option | 4-hour session, STT only |
| --- | --- |
| Soniox `stt-rt-v5` (diarization included) | $0.48 |
| AssemblyAI Universal-Streaming | $0.60 |
| AssemblyAI Universal-Streaming + diarization | $1.08 |
| ElevenLabs Scribe v2 Realtime | $1.56 |
| Deepgram Nova-3 streaming (regular) | $1.85 |
| AWS Transcribe streaming | $2.40 |
| Azure real-time | $4.00 |
| OpenAI `gpt-live-transcribe` | $4.08 |

Streaming through our backend adds a Cloud Run instance kept active for the whole
session: up to $0.29–$0.38 per 4-hour session at low scale, amortized as
concurrency grows. It also forces reconnects at the 60-minute timeout.

Browser-direct streaming with vendor temporary tokens avoids that instance cost.
It weakens server-side enforcement of pause and consent, because some vendors do
not terminate open streams when a token expires. That trade-off belongs to
`LSA-1.11`/`LSA-12.6`.

## 5. Model and retrieval cost

**Extraction** (per listening hour):

- **Cadence (matches master plan §5.6):**
  - a cheap lexicon and question detector runs on every utterance at no model
    cost;
  - small-LLM extraction runs at most once per 30 seconds, and only when the
    detector fired;
  - the model assumes the detector fires in every 30-second window, which makes
    this an upper bound for the debounced design.
- **Workload per call:** the last 2 minutes of transcript, rolling entity state,
  and a 1,500-token static prefix; 200 output tokens.
- **Volume:** about 120 calls, 292K input tokens, and 24K output tokens per
  hour.
- **Sensitivity:** without the debounce, about 300 calls per hour would cost
  about $0.099/h for `gpt-4o-mini`, versus $0.045/h. The expected listening hour
  would rise to about $0.38. `LSA-8.4` alerts on cost-per-hour drift.

| Extraction model | Uncached | Static prefix cached |
| --- | --- | --- |
| `gpt-4o-mini` | $0.058 | **$0.045** (expected) |
| `gpt-5.6-luna` (effort none) | $0.087 | $0.056 |
| `gpt-5.4-nano` | $0.088 | $0.056 |
| Gemini 2.5 Flash-Lite | $0.039 | n/a |
| `gpt-4.1-mini` | $0.155 | $0.101 |
| `gpt-5.4-mini` | $0.327 (stress) | $0.206 |
| Claude Haiku 4.5 | $0.412 | n/a (4,096-token minimum) |

The low scenario assumes the lexicon gate halves LLM calls: $0.023/h.

**Card composition** assumes 10 cards per hour at 4K input and 500 output
tokens: `gpt-4o-mini` $0.009 (expected), `gpt-5.6-terra`-class $0.14 (stress).
Entity and location cards are deterministic templates with no LLM cost.

**Pre-storage classifier** (Phase 3 redaction, master plan §5.6):

- Utterances wait in a 20-second in-memory hold-back buffer. Personal-data
  patterns run first at no model cost.
- One small-LLM call per 20-second micro-batch then labels out-of-game chatter
  and personal data before anything is stored or sent to extraction: at most
  180 calls per hour, about 570 input and 30 output tokens each.
- Cost per listening hour: $0.012 (Gemini 2.5 Flash-Lite) / **$0.019**
  (`gpt-4o-mini`) / $0.101 (`gpt-5.4-mini`).
- The same classifier runs over uploaded transcripts (per transcript hour) and
  once per push-to-talk clip.
- Consent-concern and safety phrases use an in-memory lexicon with no model
  cost.
- If the consent-concern lexicon misses its recall target (`LSA-9.5`), a
  synchronous per-utterance classifier is added, because the 20-second
  micro-batch cannot meet the 2-second pause budget. At about 300 calls per
  listening hour, with ~400 input and ~5 output tokens each, that adds roughly
  $0.02/h expected (`gpt-4o-mini`). It is left out of the tables below because
  it is conditional.

**Retrieval** is effectively free at this scale:

- query embeddings about $0.0001/h;
- exact per-campaign vector search runs inside the existing Cloud SQL instance;
- no web search from transcripts.

**Overhead** for retries, validation failures, and variance: +5% / +10% / +30%
on STT and AI.

## 6. Realtime connection cost

| Component | Low | Expected | Stress | Basis |
| --- | --- | --- | --- | --- |
| Clip/utterance request compute | $0.002 | $0.004 | $0.016 | ≈300 chunk requests/hour × ~2 s in flight × active rates; shared instances (expected) vs. none (stress) |
| Participant indicator and card connections | $0.005 | $0.02 | $0.095 | Stress assumes one 1 vCPU/1 GiB instance kept active for the session alone |
| Fanout service (Phase 2+) | — | $22.48/month fixed | $35.77/month | Valkey custom-pico; Redis Basic |

## 7. Storage and retention cost

A listening hour produces about 40–70 KB of transcript text. With encryption and
index overhead, call it about 0.2 MB. At 30-day retention on SSD, that is well
under $0.0001 per listening hour.

Raw audio is never stored in live modes. Uploaded recordings live at most
24 hours in temporary storage: about 11 MB per hour at Opus 24 kbps, which is
negligible.

KMS, logging, and scheduling together are under $0.003/h (stress) plus fixed
fees.

## 8. Image delivery cost

Portrait derivatives are about 150 KB (WebP). Views per listening hour:
10 (low) / 20 (expected) / 200 (stress), for example 40 NPC introductions
× 5 players.

At $0.12/GiB Premium Tier egress through Cloud Run, that is
$0.0002 / $0.0004 / $0.0036 per hour. A CDN adds an $18.25/month fixed
forwarding rule and only pays off at large scale. Serving authorized derivatives
through Cloud Run is cheaper and keeps authorization in one place.

## 9. Unit totals

### 9.1 Per listening hour

| Scenario | STT | Extraction + cards + classifier + embeddings | Overhead | Infrastructure | **Total** |
| --- | --- | --- | --- | --- | --- |
| Low | $0.063 | $0.041 | $0.005 | $0.008 | **$0.116** |
| Expected | $0.197 | $0.073 | $0.027 | $0.025 | **$0.322** |
| Stress | $0.282 | $0.569 | $0.255 | $0.118 | **$1.224** |

### 9.2 Per push-to-talk clip

| Scenario | Speech | STT | Extraction, composition, and classifier | **Total** | 100 clips |
| --- | --- | --- | --- | --- | --- |
| Low | 8 s | $0.00023 | $0.00065 | **$0.0010** | $0.10 |
| Expected | 12 s | $0.00097 | $0.00097 | **$0.0022** | $0.22 |
| Stress | 25 s | $0.00261 | $0.01436 | **$0.0221** | $2.21 |

---

## 10. Expected usage distributions

### 10.1 How tables play (public polls)

The only broad public data found are self-selected social-media polls run by
Sly Flourish, a D&D 5e GM author, so treat them as directional.

| Measure | Result | Poll |
| --- | --- | --- |
| Session length | Under 2 h: 2% · 2–3 h: 42% · about 4 h: 47% · 5 h or more: 9% | April 2021, n=2,152 |
| Session frequency | More than weekly: 17% · about weekly: 38% · about twice a month: 24% · monthly or less: 21% | October 2019, n=1,816 |
| Group size (players, excluding the GM) | Fewer than 4: 18% · 4: 39% · 5: 28% · 6: 11% · more than 6: 4% | September 2022, n=4,400 |
| Where people play | Mostly in person: 46% · mostly online: 41% · both: 13% | April 2023, n=2,900 |
| Generative AI use | 30% say they use it | April 2026, n=2,200 |

**Bias.** The poll author acknowledges these audiences are self-selected and
lean toward engaged 5e GMs. The polls come from different years (before, during,
and after the pandemic), and wording varies. No industry or academic source with
session-length or group-size data could be retrieved.

**What the model takes from them:**

- a weekly 4-hour game produces about 17 listening hours a month per campaign,
  if the whole session were captured;
- about half of groups play mostly in person, so a single device at the table
  is the main capture path;
- roughly 70% of this audience does not use generative AI, so an opt-in,
  push-to-talk-first product fits the market better than always-on capture.

### 10.2 Personas

**Assumptions to replace with measured canary data** (`LSA-8.4`). Listening
hours are capped at each plan's allowance.

| Persona | Plan | Clips/month | Listening h/month | Transcript-upload h | Share of live-enabled subscribers (assumption) |
| --- | --- | --- | --- | --- | --- |
| Light GM (PTT only) | Base $15 | 40 | 0 | 0 | 55% |
| Typical GM | Base $15 | 100 | 1 (trial allowance) | 2 | 30% |
| Typical premium GM | Premium $25 | 150 | 8 | 2 | 8% |
| Heavy GM at the allowance | Premium $25 | 150 | 12 (capped) | 0 | 5% |
| Power GM at the allowance | Premium $25 | 400 | 12 (capped) | 4 | 2% |
| *Alternative tier:* weekly 4-hour table | Premium $29 | 150 | 17.3 | 0 | — |
| *Alternative tier:* power GM at the allowance | Premium $29 | 400 | 20 (capped) | 4 | — |

**Monthly live COGS by persona** (excludes base chat COGS):

| Persona | Low | Expected | Stress |
| --- | --- | --- | --- |
| Light GM (PTT only) | $0.04 | $0.09 | $0.88 |
| Typical base GM | $0.30 | $0.70 | $4.92 |
| Typical premium GM | $1.16 | $3.07 | $14.59 |
| Heavy GM at the $25 allowance (12 h) | $1.54 | $4.19 | $18.00 |
| Power GM at the $25 allowance (12 h) | $1.96 | $5.06 | $26.50 |
| Weekly 4-hour table on $29 (17.3 h) | $2.16 | $5.90 | $24.48 |
| Power GM at the $29 allowance (20 h) | $2.89 | $7.64 | $36.28 |

Participants and guests do not consume capture allowances. Capture is attributed
to the GM account that owns the campaign.

### 10.3 Market pricing anchors

Competitors found in research (September 2026) all transcribe **after** the
session. None was found delivering live, permission-aware cards to players.
Prices are vendor list prices, not verified contracts.

| Product | Unit | Price |
| --- | --- | --- |
| GM Assistant | Audio hours | About $0.72 per hour pay-as-you-go; $9/month (about 12.5 h); $25/month (about 33 h) |
| Tabletop Scribe | Audio-hour credits | $1.10 per hour (1), $1.00 (10), $0.90 (50) |
| Archivist | Sessions | $10 (4 sessions), $20 (10), $35 (20), $60 (40) per month |
| Saga20 | Sessions | $12.99 (6), $19.99 (12), $39.99 (24) per month |
| SessionKeeper | Campaigns | $3.99–$39.99 per month; no hour caps stated |
| The DM's ARK | Campaigns | $6–$15 per month |

**Reading.**

- At the $25 hypothesis, the $10 premium over base buys 12 listening hours:
  about $0.83 per hour of allowance.
- That sits inside the $0.72–$1.10 per audio hour post-session tools charge,
  while the base plan's chat and push-to-talk come on top.
- A $29 / 20-hour tier works out to $0.70 per hour, at the bottom of that range.
- Transcription volume alone does not differentiate the product. The premium
  must be justified by live cards, campaign-aware retrieval, and player
  displays.

## 11. Per-plan allowances (proposal for `LSA-1.9`)

| Capability | Base ($15 hypothesis) | Premium "Live Table" ($25–29) | Sponsored/promotional accounts |
| --- | --- | --- | --- |
| Push-to-talk | 100 clips/month (≈20 audio minutes) | 600 clips/month | 50 clips/month |
| Timed listening (Phase 2+) | 1 listening hour/month (trial) | 12 h at $25, or 20 h at $29 | None |
| Automatic GM suggestions (Phase 3+) | No | Yes | No |
| Transcript upload | 2 transcript hours/month | 10 transcript hours/month | None |
| Audio upload (Phase 3+) | No | Counts against listening hours | No |
| Session listening, up to 4 h (Phase 5) | No | Yes, within hours | No |
| Player displays of live cards (Phase 4+) | GM displays only | GM displays + GM-authored re-display rules (Phase 5) | GM displays only |
| Hard monthly COGS cap (all features incl. chat) | $3.00 | $12.00 | Billing plan sponsored cap |
| Reserved chat headroom inside the cap (proposal) | $1.50 | $1.50 | Billing plan sponsored target |
| Daily listening cap | 1 h | 6 h | 0 |

Allowances are **product units**. The binding control is the **cost
reservation** against the monthly COGS cap, provided by the shared reservation
service `yje.5.2`, which `LSA-8.2` extends with live allowances and chat
headroom. If vendor prices rise, the cap throttles usage before allowances run
out, and the exhaustion message states that plainly.

## 12. Overage and throttling policy

1. **No automatic overage charges in v1.** Metered add-ons require billing
   support for usage products and are revisited after Phase 3
   (`LSA-8.3`/`yje`).
2. **At 80%** of an allowance or of the COGS cap: a banner in pre-flight and in
   the listening banner.
3. **At 100%:**
   - listening modes stop at the next utterance boundary with a clear message;
   - push-to-talk continues while its own allowance and the cap allow.
4. **Chat headroom and shedding order (proposal for `LSA-1.9`, amending the
   billing plan through `yje.1.2`).**
   - The billing plan's shedding order today is: automatic web search, premium
     model routing, extra structuring and suggestions, media generation. **It
     does not mention live usage.**
   - Because the monthly cap includes chat, heavy early listening could starve
     chat later in the month.
   - Proposal:
     1. Live reservations may not use the reserved chat headroom (§11).
     2. Automatic GM suggestions shed together with "extra structuring and
        suggestions".
     3. Timed and session listening and audio upload shed together with media
        generation.
     4. Push-to-talk and transcript upload shed last among optional features.
     5. Core chat and account data access are never shed by live usage.
5. **Daily cap** prevents burst abuse. There is one capture window at a time per
   live table session, and a GM has at most one live session (Workbench
   REVEAL-2).
6. **Kill switches** (`LSA-8.4`, operating through the flags in `LSA-13.8`):
   - global listening off;
   - vendor-specific STT off;
   - automatic suggestions off;
   - push-to-talk off.

   Each is independent.
7. **Alert thresholds** (initial):
   - expected cost per listening hour > $0.45 for 3 days;
   - p95 account live COGS > 80% of the plan cap;
   - any account exceeding its reservation (bug).

## 13. Profitability

### 13.1 Per subscriber (base chat COGS included; COGS caps applied)

| Plan (allowance) | Persona | Low | Expected | Stress |
| --- | --- | --- | --- | --- |
| Base $15 | Light GM | $13.05 (87.0%) | $12.50 (83.3%) | $11.09 (73.9%) — cap binds (raw $3.88) |
| Base $15 | Typical GM | $12.78 (85.2%) | **$11.88 (79.2%)** | $11.09 (73.9%) — cap binds (raw $7.92) |
| Premium $25 (12 h) | Typical premium GM (8 h) | $21.51 (86.0%) | **$19.11 (76.4%)** | $11.68 (46.7%) — cap binds (raw $17.59) |
| Premium $25 (12 h) | Heavy GM at the allowance | $21.13 (84.5%) | **$17.98 (71.9%)** | $11.68 (46.7%) — cap binds (raw $21.00) |
| Premium $25 (12 h) | Power GM at the allowance | $20.71 (82.9%) | $17.11 (68.5%) | $11.68 (46.7%) — cap binds (raw $29.50) |
| Premium $29 (20 h) | Weekly 4-hour table (17.3 h) | $24.35 (84.0%) | $20.11 (69.4%) | $15.51 (53.5%) — cap binds (raw $27.48) |
| Premium $29 (20 h) | Power GM at the allowance | $23.62 (81.4%) | $18.37 (63.4%) | $15.51 (53.5%) — cap binds (raw $39.28) |
| Premium $19 (12 h) | Heavy GM at the allowance | $15.38 (80.9%) | $12.23 (64.4%) | $5.92 (31.2%) — cap binds (raw $21.00) |

Values are contribution dollars with contribution margin in parentheses. Raw
figures are total COGS before the cap.

**Stress reading.** Under stress unit costs the caps bind for every persona, so
margin is bounded by construction. In practice, stress-priced users get fewer
listening hours than advertised. If a price shock lasts, the fix is a vendor or
model change or a new price revision, not a larger cap.

### 13.2 Portfolio (1,000 live-enabled subscribers: 85% base at $15, 15% premium at $25 with 12 hours)

Persona shares follow §10.2.

| Scenario | Revenue | Fees | COGS | Contribution | Margin | Of which live COGS |
| --- | --- | --- | --- | --- | --- | --- |
| Low | $16,500 | $977 | $1,321 | $14,202 | 86.1% | $321 |
| Expected | $16,500 | $977 | $2,315 | **$13,208** | **80.1%** | $815 |
| Stress | $16,500 | $977 | $4,350 | $11,174 | 67.7% | $1,350 |

The stress portfolio falls below the 70% target because every cap binds. It
stays bounded and profitable, which the billing plan's launch rule requires, but
a lasting price shock needs a vendor, model, or price revision.

### 13.3 Premium upgrade economics (incremental over base $15, heavy persona at the plan's allowance)

| Premium price (allowance) | Low | Expected | Stress |
| --- | --- | --- | --- |
| $19 (12 h) | +$2.59 | **+$0.35** | −$5.16 |
| $25 (12 h) | +$8.35 | **+$6.10** | +$0.59 |
| $29 (20 h; 17.3 h used) | +$11.57 | **+$8.23** | +$4.43 |

### 13.4 Fixed and one-off costs

**Recurring infrastructure attributable (wholly or partly) to live features:**
about **$64/month**:

- Valkey fanout $22.48, if the Workbench realtime decision (`1kg.1.4`) picks it
  over Postgres `LISTEN/NOTIFY` or Pub/Sub. If the Workbench already pays for
  fanout, the live increment is near zero.
- Cloud SQL production-tier delta $41.64, shared with the Workbench.
- KMS and Scheduler under $1.

**Billing kill switch.**

- Production currently runs under a $10 GCP budget. Its kill switch detaches
  billing from the whole project, halting every billable resource
  (`docs/deploy-gcp.md`).
- The Cloud SQL instance alone uses about $9.40 of that budget (`yje.6.6`
  scope), so even Phase 1's small recurring items could trip it:
  - Cloud KMS keys (`LSA-6.1`);
  - scheduled retention jobs (`LSA-6.2`);
  - one Secret Manager secret per database role (`LSA-1.13`, `LSA-2.3`).
- Tripping it detaches billing and takes the service offline.
- `yje.6.6` replaces the switch with production spend controls. It blocks the
  Phase 1 gate (`LSA-13.2`) and `LSA-9.3`, the first bead that needs shared
  fanout.
- Building and testing live features adds no production spend. The `LSA-13.8`
  rollout runbook provisions live production resources only after `yje.6.6`
  has shipped, and deploy verification checks projected spend against the
  active budget guard.

**One-off gate costs** (legal review, external security review):

| One-off placeholder (not a quote) | Monthly over 12 months (incl. recurring) | Premium upgrades at $25 to break even (typical premium persona, expected) |
| --- | --- | --- |
| $10,000 | $898 | 125 |
| $25,000 | $2,148 | 298 |
| $50,000 | $4,231 | 586 |

**Implication.** Push-to-talk needs the Phase 0 legal review but no external
player-path review. Validate paid demand for listening and player cards with
the push-to-talk MVP and the Phase 2 canary before committing to the Phase 4
external security review.

## 14. Push-to-talk in base, continuous listening in premium?

**Yes.**

| Consideration | Push-to-talk | Continuous (timed/session) listening |
| --- | --- | --- |
| Expected COGS per active GM | $0.09–$0.22/month | $3.07–$4.19/month within the $25 allowance; $5.90 for a weekly 4-hour table |
| Stress COGS | $0.88–$2.21/month | $15–$27/month of live COGS within the $25 allowance, bounded by the $12 cap |
| Privacy exposure | Seconds of deliberate capture | Minutes to hours of ambient table audio |
| Consent complexity | Per-participant consent, roster, and announcement in a table session | The same, plus announcements at every resume, late-joiner pauses, safety signals, and auto-pause |
| Infrastructure | Request/response | Realtime state fanout, reconnects |
| Product value | Fast lookups, low interruption | Proactive suggestions, recaps |
| Abuse/cost tail | Small | Large without caps |

Offering push-to-talk in base drives adoption and trust at negligible cost.
Continuous listening carries the cost tail, the legal surface, and the
infrastructure, so it belongs behind a premium price that funds its gates.

## 15. Model risks and sensitivities

| Risk | Effect | Mitigation |
| --- | --- | --- |
| OpenAI `gpt-4o-*-transcribe` and `whisper-1` shutdown (2027-02-26) | Any adoption of those models forces migration within months | Start on `gpt-transcribe` or a non-deprecated vendor; adapter abstraction (`LSA-4.3`) |
| Retiring small LLMs (`gpt-4.1-nano` 2026-10-23, `gpt-5-nano` 2026-12-11) | Cheapest extraction options vanish | Benchmark only models without announced shutdowns |
| Gemini 3.x Flash price doubles 2027-01-01 | 2× extraction cost if chosen | Price revisions with effective dates; alerting |
| Deepgram promo vs regular price | 60% higher streaming cost at regular price | Budget on regular price |
| Vendor default retention/training (Deepgram, AWS, ElevenLabs) | ZDR may need opt-out flags or enterprise tiers at higher cost | Include opt-out requirements and any price uplift in `LSA-1.5` |
| Reasoning-token defaults (`gpt-5.6-*` effort medium) | Output tokens silently multiply | Pin reasoning effort; cost-per-call regression alert |
| Data-residency uplifts (OpenAI +10% for models released ≥ 2026-03-05; Anthropic 1.1× US inference) | +10% model cost | Include in price revisions if residency is required |
| Speech fraction higher than modeled | STT scales linearly | Measure in canary; VAD tuning |
| Usage mix skews heavy | Premium margin falls toward the cap floor | Allowance sizing; cap-bound margins; price revision |
| Personas rest on self-selected community polls (§10.1) | Real session length, frequency, and share of power users may differ | Replace with canary measurements (`LSA-8.4`) before any price decision |
| Extraction cadence drifts from the debounce | Per-utterance calls push the expected listening hour to about $0.38 | Debounce enforced in extraction (`LSA-4.5`); cost-per-hour alert (`LSA-8.4`) |
| Self-hosted or on-device speech-to-text chosen for privacy (`LSA-1.5`, threat model §6.10) | GPU hosting or device-performance costs are **not modeled**; fixed costs could rise sharply | Model hosting costs inside `LSA-1.5` before selection |
| Project-wide $10 GCP billing kill switch | Even small new recurring items (keys, jobs, secrets) could detach billing and take production offline | `yje.6.6` blocks the Phase 1 gate (`LSA-13.2`) and `LSA-9.3`; deploy verification checks projected spend (`LSA-13.8`) |

## 16. Reproducing the model

The calculator (`docs/forge/tools/live-session-assistant/costs.ts`) applies these
formulas; its recorded output is `cost-tables.md` in the same folder.

```text
listening_hour = STT + AI + overhead + infra
  STT      = 60 × speech_fraction × vad_padding × stt_price_per_min
  AI       = extraction_per_hour + cards_per_hour + classifier_per_hour + embeddings_per_hour
  overhead = (STT + AI) × overhead_rate
  infra    = request_compute + realtime_connections + storage_kms_logs + images

ptt_clip = ((clip_seconds × vad_padding / 60) × stt_price_per_min
            + extraction_per_clip + compose_share × compose_cost + classifier_per_call) × (1 + overhead_rate)
            + request_compute

listening_hours   = min(persona_hours, plan_allowance_hours)
monthly_live_cogs = clips × ptt_clip + listening_hours × listening_hour
                    + upload_hours × transcript_upload_hour
fees              = price × 4.1% + $0.30
contribution      = price − fees − min(live_cogs + base_chat_cogs, plan_cap)
```

The scenario inputs are the values in §3–§8. Replace them with production price
revisions and canary measurements before any pricing decision (`LSA-1.9`).

## 17. Sources (accessed 2026-09-16)

- **Speech-to-text:**
  - https://developers.openai.com/api/docs/pricing
  - https://developers.openai.com/api/docs/models/gpt-transcribe
  - https://developers.openai.com/api/docs/deprecations
  - https://deepgram.com/pricing
  - https://www.assemblyai.com/pricing
  - https://soniox.com/pricing
  - https://elevenlabs.io/pricing/api
  - https://aws.amazon.com/transcribe/pricing/ (AWS Price List API)
  - https://azure.microsoft.com/en-us/pricing/details/speech/ (Azure Retail Prices API)
  - https://ai.google.dev/gemini-api/docs/pricing
- **LLMs:**
  - https://developers.openai.com/api/docs/pricing
  - https://developers.openai.com/api/docs/guides/your-data
  - https://platform.claude.com/docs/en/about-claude/pricing
  - https://ai.google.dev/gemini-api/docs/pricing
- **Google Cloud:**
  - https://cloud.google.com/run/pricing
  - https://docs.cloud.google.com/run/docs/triggering/websockets
  - https://docs.cloud.google.com/run/docs/configuring/request-timeout
  - https://cloud.google.com/sql/pricing
  - https://cloud.google.com/storage/pricing
  - https://cloud.google.com/cdn/pricing
  - https://cloud.google.com/vpc/network-pricing
  - https://cloud.google.com/pubsub/pricing
  - https://cloud.google.com/memorystore/docs/valkey/pricing
  - https://cloud.google.com/memorystore/docs/redis/pricing
  - https://cloud.google.com/kms/pricing
  - https://cloud.google.com/stackdriver/pricing
  - https://cloud.google.com/scheduler/pricing
  - https://cloud.google.com/tasks/pricing
- **Payments:**
  - https://stripe.com/pricing
  - https://stripe.com/billing/pricing
- **Speaking rate:**
  - Yuan, Liberman & Cieri (2006), Interspeech — https://www.isca-archive.org/interspeech_2006/yuan06_interspeech.pdf
  - NCVS — https://ncvs.org/archive/ncvs/tutorials/voiceprod/tutorial/quality.html
- **How tables play (§10.1):**
  - Sly Flourish poll archive — https://slyflourish.com/facebook_surveys.html
  - Sample-bias note by the poll author — https://slyflourish.com/errors_in_dnd_data_analyses.html
- **Market pricing anchors (§10.3; vendor list prices):**
  - GM Assistant — https://gmassistant.app/
  - Tabletop Scribe — https://www.tabletopscribe.com/
  - Archivist — https://www.myarchivist.ai/pricing
  - Saga20 — https://saga20.com/pricing/
  - SessionKeeper — https://www.sessionkeeper.ai/pricing
  - The DM's ARK — https://thedmsark.com/
- **Billing kill switch:** `docs/deploy-gcp.md` in this repository; billing plan
  `docs/forge/plans/subscription-billing-coupons-profitability.md`
