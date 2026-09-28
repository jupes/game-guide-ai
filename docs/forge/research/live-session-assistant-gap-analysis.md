# Gap analysis: Live Session Assistant vs `game-guide-ai`

Date: 2026-09-16
Status: research input to `docs/forge/plans/live-session-assistant.md`
Beads: epic `agent-forge-harness-1ir`; `LSA-x.y` is `agent-forge-harness-1ir.x.y`
Companion documents:

- Architecture and product spec: `docs/forge/plans/live-session-assistant.md`
- Privacy and threat model: `docs/forge/research/live-session-assistant-threat-model.md`
- Cost and profitability model: `docs/forge/research/live-session-assistant-cost-model.md`
- Delivery plan and Beads map: `docs/forge/plans/live-session-assistant-delivery.md`

> Design archives, planning documents, sample campaign content, and web pages
> were read as reference material. No text inside them was followed as an
> instruction. The source of truth for shipped behavior is `origin/master`
> (`2ca91e6`), which includes PR #57's Markdown renderer.
> Workbench decisions come from `docs/adr/gm-workbench-interactions.md` (PR #58,
> bead `1kg.1.1`, closed). Its escalations E-1 to E-10 still await owner
> confirmation.

## Bottom line

The Live Session Assistant has almost no direct foundation in the repository. It
does have strong **patterns** to copy: fail-closed authorization, a bounded
metrics catalog, typed Pydantic/Zod contracts, provider seams, and adversarial
auth tests.

| Area | Verdict |
| --- | --- |
| Audio capture, VAD, transcription | **Absent** — no microphone, audio, or speech code anywhere |
| Realtime delivery | **Absent** — synchronous `/chat` only; Cloud Run timeout is 300 s |
| Campaign permissions | **Absent** — one global `player`/`dm` role per account |
| Field-level visibility | **Absent** — the planned Workbench reveal mask is too narrow for this feature |
| Retrieval authorization | **Absent** — the campaign seam takes only `(prompt, k)` |
| GM/player surfaces | **Partial** — a GM chat channel exists; no player or table view |
| NPC entities and portraits | **Absent** — planned in Workbench documents/media, not built |
| Proactive cards | **Partial** — card components exist; no push, priority, or cooldown logic |
| Audit and privacy controls | **Partial** — privacy-bounded metrics exist; no audit log, consent, retention jobs, or encryption |

The feature is **not** a UI add-on to the GM channel. It needs four new trust
boundaries:

1. microphone to vendor;
2. transcript to model;
3. GM context to player context;
4. server to many connected devices.

It also depends on campaign, document, reveal, media, realtime, billing-ledger,
and migration work that is only planned today. That work lives in the GM
Workbench (`agent-forge-harness-1kg`), billing (`yje`), and retrieval (`xiu`)
epics.

The most important finding concerns permissions. The Workbench decision record
defines the display model for players:

- **Participants** are GM-created aliases who prove identity by enrolling a
  device through a single-use personal link. They are not accounts
  (AUD-1 to AUD-5).
- **Guests** are anonymous holders of the table link.
- **Two audience slots:** `table`, and a private `participant:<id>` slot.
- **One live document per slot**, with a pinned version and no player-side
  history (REVEAL-7/8, E-2/E-3).

That model gets display right: explicit, pinned, stoppable. It does not express
*eligibility*. It has no field classes for groups, characters, campaign-only
versus public, or unclassified. It also has no durable disclosure ledger.

This plan extends it rather than competing with it (`LSA-1.2`), and that decision
blocks the Workbench beads where audience schemas freeze (`1kg.2.1`, `1kg.5.1`,
`1kg.5.3`, `1kg.7.1`). Two permission systems would be the most likely path to a
secret leak.

A second finding is already shipping. PR #57's Markdown renderer keeps remote
images (DOMPurify defaults, no CSP; open bug `va8`). Model-written card text
could be steered into sending GM-only text to another host. `va8` must be fixed
before any card renders (`LSA-4.6`, Phase 1 gate).

## Inputs reviewed

### Repository (read, not executed)

- Service:
  - `service/app.py` — routes, auth dependency, `/chat`, ownership, and error taxonomy
  - `service/session.py`, `service/auth_store.py`
  - `service/rag.py`, `service/graph.py`, `service/generate.py`
  - `service/history.py`, `service/attachments.py`, `service/metrics.py`
  - `service/tracing.py`, `service/gcp_logging.py`, `service/ratelimit.py`
  - `service/model_catalog.py`, `service/providers.py`
- Schemas: `service/sql/04-chat-schema.sql`, `service/sql/05-auth-schema.sql`, `vector-db/init/*.sql`
- Configuration and deployment: `config.py`, `scripts/deploy.sh`, `Dockerfile.cloud`, `ui/nginx.conf`, `docs/deploy-gcp.md`
- UI: `ui/src/shell/*` (modes, current user, conversation store, chat pane), `ui/src/ds/*` (content cards, Avatar), `ui/src/useChat.ts`
- Test inventory in `service/tests/` — role enforcement, conversation ownership, auth guard, and metrics tests are the patterns to extend.

### Planning context

- `docs/forge/plans/aetheril-gm-workbench-expansion.md` and `docs/forge/research/aetheril-gm-workbench-gap-analysis.md` (`1kg`)
- `docs/forge/plans/additive-retrieval-web-fallback.md` (`xiu`)
- `docs/forge/plans/subscription-billing-coupons-profitability.md` (`yje`)
- `docs/forge/plans/game-guide-ai-memory-personalization.md` (`1ka`) — retention and tracing-privacy decisions M-D11 and M-D15
- `docs/forge/plans/game-guide-ai-model-routing.md` (`b8o`) — provider posture
- `docs/adr/gm-workbench-interactions.md` (PR #58): Workbench decisions X-1 to X-10, REVEAL-*, TABLE-*, AUD-*, and escalations E-1 to E-10
- Live Beads state at planning time: 152 issues across `1kg`, `xiu`, `yje`, `1ka`, `b8o`, `iu6`, plus follow-ups `va8` (Markdown remote images, no CSP) and `764`

### Design reference (`Aetheril game content cards.zip`, expanded read-only)

- `aetheril-gm-workbench/docs/patterns.md`, `docs/wire-schema.md`, `docs/INTEGRATION.md`
- `tools/documentTypes.js`, `tools/toolRegistry.js`
- `components/canvas/RevealSheet.*`, `components/canvas/GameDocument.*`
- `components/dnd/AudioCue.*`, `components/dnd/AssistantLane.*`

The archive contains **no** designs for microphone capture, listening
indicators, recording consent, transcripts, proactive cards, or player-side AI
output.

## Capability matrix

Legend: **Present** · **Partial** · **Planned** (bead exists, no code) · **Absent**

| Capability | Current evidence | Status | Owner of the gap |
| --- | --- | --- | --- |
| Microphone capture in browser | No `getUserMedia`, `MediaRecorder`, or `AudioWorklet` in `ui/src` | Absent | This initiative |
| Audio upload | Attachments accept only `.txt/.md/.pdf` and discard bytes (`service/attachments.py:5,37`) | Absent | This initiative |
| Speech-to-text | No provider, adapter, or vocabulary support | Absent | This initiative (vendor decision) |
| Voice activity detection | None | Absent | This initiative |
| Browser mic policy headers | No `Permissions-Policy`/CSP in `ui/nginx.conf` or FastAPI | Absent | This initiative + `1kg.9.5` |
| Streaming/realtime transport | `/chat` is synchronous JSON; `useChat` allows one request in flight (`ui/src/useChat.ts:114,173`) | Absent | `1kg.1.4` ADR + this initiative |
| Long-lived connections on Cloud Run | `--timeout 300`, `--max-instances 2`, `--concurrency 20` (`scripts/deploy.sh:152-155`) | Absent | `1kg.1.4`, `1kg.9.5` |
| WebSocket proxying | Compose nginx has no `Upgrade`/`Connection` handling and `proxy_read_timeout 120s`. Production Cloud Run runs a single uvicorn process serving the SPA and API, with no nginx hop. | Absent | `1kg.9.5` |
| Safe rendering of model output | `ui/src/components/Markdown.tsx` (PR #57) sanitizes with DOMPurify defaults, which keep remote `<img>`; no CSP on either host | **Unsafe** (open bug `va8`) | `va8`, then `LSA-4.6` |
| Cross-instance fanout | No outbox, pub/sub, or shared cache; limiter is per-instance memory (`service/ratelimit.py:21`) | Absent | `1kg.1.4`, `1kg.1.5` |
| Account roles | `auth.users.role IN ('player','dm')` (`service/sql/05-auth-schema.sql`) | Present (global only) | — |
| Server role enforcement | GM mode 403 for non-DM (`service/app.py:731-732`) | Present | Reuse pattern |
| Campaign/participant/character/group model | None. Decision record: participants are aliases with enrolled devices; character sheets link to one participant; no groups | Planned (`1kg.2.1`) — no groups | `1kg.2.*` + this initiative |
| Live table session and join token | None. Decision record: one live session per campaign; table token in URL fragment exchanged for a cookie | Planned (`1kg.2.3`) | `1kg.2.3` |
| Field eligibility, display, and disclosure | None. Decision record: table and participant slots, pinned versions, explicit masks, Stop narrows immediately; no eligibility classes or ledger | Display planned (`1kg.7.1`); eligibility absent | **Shared decision** (`LSA-1.2` blocks `1kg.2.1`/`5.1`/`5.3`/`7.1`) |
| Disclosure ledger / knowledge grants | None | Absent | This initiative |
| Authorization-aware retrieval | `SecondaryRetriever.retrieve(prompt, k)` (`service/rag.py:44-46`); stub returns empty | Absent | This initiative + `xiu.2.5` |
| Evidence provenance | Binary `answerable`; sources carry no visibility or audience | Planned (`xiu.1.2`) | `xiu.1.2` |
| Untrusted-context delimiting | `GROUNDED_TEMPLATE = "Sources:\n{context}\n\nQuestion: …"` (`service/generate.py:209`) | Absent | `xiu.2.4` + this initiative |
| NPC/location/quest entities | None; documents planned | Planned (`1kg.5.*`) | `1kg.5.*` + entity index here |
| Entity aliases and fuzzy/phonetic matching | Corpus vocabulary matcher for rules entities only (`ingestion/retrieval.py:128,308`) | Partial (rules only) | This initiative |
| Media/portrait storage | None; `Avatar` accepts a URL | Planned (`1kg.8.1`) | `1kg.8.1` + audience derivatives here |
| Proactive card rendering | `GameContentCard`, `SpellCard`, `StatBlockCard` | Partial | Reuse |
| Card lifecycle (priority, cooldown, dismiss, correct) | None | Absent | This initiative |
| Push notifications to GM/player screens | None | Absent | This initiative |
| Player/table UI | None; GM channel hidden for players (`ui/src/shell/modes.ts:46`) | Planned (`1kg.7.4`) | `1kg.7.4` + player cards here |
| Consent records and indicators | None; handoff `AudioConsentPrompt` is a **playback** autoplay tap | Absent | This initiative |
| Audit log | None; only operational logs and metrics | Absent | This initiative |
| Retention/deletion jobs | None; account delete cascades (`05-auth-schema.sql`) | Planned (`1ka.5`, `1kg.2.6`) | Shared |
| Application-level encryption | None (platform encryption at rest only) | Absent | This initiative |
| Content-free tracing | Langfuse `CallbackHandler` sends full prompts when `RAG_TRACING=1` (`service/tracing.py:66-77`) | Absent | This initiative + `1ka` M-D11 |
| Cost metering per operation | Count-based limits: 20/h per user, 500/day global (`config.py:230,235`) | Planned (`yje.5.1`, `yje.5.2`) | `yje.5.*` |
| Entitlements | None | Planned (`yje.4.1`) | `yje.4.1` |
| Ordered migrations | Idempotent startup SQL only | Planned (`1kg.1.5`) | `1kg.1.5` |
| Adversarial authz tests | `test_role_enforcement.py`, `test_conversation_ownership.py`, `test_auth_guard.py` | Partial (patterns) | This initiative + `1kg.9.1` |

## 1. Audio and microphone support

**Current state.** No code captures, uploads, encodes, stores, or transcribes
audio.

- The only upload path is conversation attachments. It accepts text and PDF,
  extracts text, and keeps no original bytes (`service/attachments.py`,
  `chat.attachments.extracted_text`).
- The design handoff's `AudioCue`/`AudioConsentPrompt` components are about
  **playing** GM sound cues and satisfying browser autoplay rules ("Tap to hear
  it"). They are unrelated to capture consent.

**Gaps.**

- Capture: permission prompts, capture state machine, level meter,
  push-to-talk, timed windows, and handling for tab-hidden, screen-lock, and
  device-change events.
- Encoding (Opus or PCM16) and bounded clip/chunk sizes.
- Client-side VAD, so silence never leaves the device.
- Speech-to-text adapter with fantasy-vocabulary hints and a vendor kill switch.
- Security headers:
  - `Permissions-Policy: microphone=(self)` on app routes;
  - `microphone=()` wherever capture is not intended;
  - a CSP that pins connect targets.
- **Naming hazard.** The handoff already uses "consent" for audio playback. The
  capture feature must use distinct terms and components ("listening consent",
  "capture window") so the two concepts are never conflated in code or UI.

## 2. Realtime or streaming infrastructure

**Current state.**

- **Request model.** `/chat` is synchronous (`service/app.py:710-863`). The UI
  allows exactly one exchange in flight (`ui/src/useChat.ts:114,173-200`).
- **Hosting.** One Cloud Run service with `--timeout 300`, `--max-instances 2`,
  `--concurrency 20`, `--memory 1Gi` (`scripts/deploy.sh:152-155`), running a
  single uvicorn process (`Dockerfile.cloud`).
- **Proxies.** Only the compose stack uses nginx, which proxies REST paths with
  `proxy_read_timeout 120s` and no WebSocket upgrade headers (`ui/nginx.conf`).
  Production has no nginx hop.
- **Rate limiting.** The limiter is in-process and per-instance by design
  (`service/ratelimit.py:21-24`).
- **Missing primitives.** There is no SSE, WebSocket, outbox, pub/sub, Redis, or
  background worker. `1kg.1.4` owns the realtime/media ADR, and `1kg.9.5` owns
  deployment wiring.

**Gaps and consequences.**

- **Connection lifetime.** A single WebSocket or SSE connection is cut at
  300 seconds under current settings. A 15- or 30-minute listening window
  therefore needs one of:
  - a resumable reconnect protocol;
  - a higher timeout on a dedicated realtime service; or
  - a transport that is not long-lived at all.

  The plan recommends VAD-segmented utterance uploads over HTTPS for Phases 1–3.
- **Multi-device delivery.** Pushing cards to the GM's second device, or showing
  capture indicators on player devices within a second, requires fanout across
  instances. Postgres `LISTEN/NOTIFY`, Pub/Sub, or Redis is undecided
  (`1kg.1.4`). Phase 1 can instead poll from the table client for push-to-talk
  (`LSA-3.6`); timed listening needs the fanout.
- **Quotas.** Per-instance limiters cannot enforce per-account audio-minute
  quotas. Quotas must be enforced from the database or cost ledger (`yje.5.2`).

## 3. Campaign permissions

**Current state.**

- **Identity.** One global role per account (`player`/`dm`). Sessions are
  signed, stateless cookies (`service/session.py`).
- **Freshness.** `require_session` re-reads the account on every request, so
  deletion and demotion take effect immediately (`service/app.py:335-361`).
  Individual sessions cannot be revoked; that is `yje.2.3`.
- **Ownership.** One user owns each conversation, via a first-writer claim
  (`service/app.py:578-619`).
- **Missing domain.** No campaigns, participants, characters, groups,
  co-GMs, or table sessions exist.

**Planned elsewhere.**

- `1kg.2.1`–`1kg.2.3`: campaigns, participants, sessions, join tokens.
- `1kg.7.1`: reveal projection and audience rules.

The decision record (PR #58) fixes the Workbench display model:

- **Parties:** one GM per campaign; participants are aliases with an enrolled
  device (AUD-1 to AUD-5, E-1, E-6); guests are anonymous (AUD-7, E-10).
- **Slots:** a table slot and one private slot per participant, each holding one
  live document with a pinned version and an explicit field mask
  (REVEAL-7/8/9, E-2/E-3).
- **Narrowing:** Stop narrows immediately, and a Stop defeats earlier widenings
  (X-3, REVEAL-22).
- **Never revealable:** tags, sources, authorship, and asset metadata
  (REVEAL-10).
- **Assets:** read per request, `no-store` (REVEAL-21).

**Gaps specific to this feature.**

| Required visibility class | Expressible with the decision record? |
| --- | --- |
| GM-only | Yes — nothing is shown until the GM reveals |
| Specific player | Yes, for display (participant slot). No eligibility rule forbids showing a field to the wrong participant. |
| Specific character | Partly: owner audience through a linked character sheet |
| Selected group | No |
| Entire campaign (participants but not guests) | No — the table slot includes anonymous guests |
| Public (guests, exports) | Yes, as the table slot, but not as a declared field class |
| Unclassified / private by default | Implicit only; not distinguishable from "deliberately GM-only" |
| Spoiler protection until GM reveals | Yes — display needs a GM action (X-2) |
| Version-pinned disclosure | Yes (REVEAL-8) |
| Durable disclosure ledger (what was shown to whom) | No (no player history, E-2; no audit ledger defined) |
| Eligibility enforced before retrieval and model input | No — the Workbench projects documents for display; it does not govern AI retrieval or generation |

Handoff defects the decision record already resolves, and which this plan
inherits:

- `defaultReveal` seeds only a staged draft (REVEAL-4).
- `'all'` is never stored (REVEAL-9).
- Reveal groups and per-field warnings come from the registry, not hard-coded
  keys (REVEAL-11).
- `data.revealed` is dropped in favor of server state (REVEAL-13).

## 4. Retrieval authorization

**Current state.**

- **Campaign seam.** The GM mode fans out to
  `svc.secondary.retrieve(state["prompt"])` (`service/graph.py:141-142`). The
  protocol is `retrieve(prompt, k)` (`service/rag.py:44-46`). It carries no
  principal, campaign, or audience, and the stub returns empty.
- **Merge.** Secondary chunks are merged into the same `RetrievalResult` with no
  provenance or visibility labels (`service/rag.py:_merge_results`).
- **Attachments.** All attachment texts for a conversation are concatenated into
  the prompt (`service/app.py:504-523`, `service/generate.py:233-256`).
- **Prompt shape.** Context is sent as `Sources:\n{context}\n\nQuestion:` with no
  untrusted-data delimiting (`service/generate.py:209`). The GM persona may
  "extrapolate, invent, and create" (`service/generate.py:198-205`).
- **Corpus.** `dnd.chunks` is a shared licensed rules corpus with no tenant
  columns (`vector-db/init/02-schema.sql`). That is correct for rules text.
  Campaign content must never be written into it.
- **Tracing.** When enabled, Langfuse receives the full graph run, including
  retrieved text and attachment context (`service/tracing.py:66-77`).

**Gaps.**

- **Authorization-bound interface.** The retrieval interface needs a principal
  and audience, and the database role must physically lack access to data the
  audience may not see. Post-filtering after retrieval is not acceptable.
- **Chunking granularity.** Chunks must be **field-granular**. A chunk that
  mixes a public description with a GM-only motive can never be served to
  players.
- **Separate namespaces.** GM-namespace and table-namespace indexes need
  separate database roles and row-level security.
- **Typed evidence.** Evidence must carry `visibility`, `audience`, and
  `version_hash` (extends `xiu.1.2`).
- **Content-free tracing** on every live pipeline (aligns with `1ka` M-D11).
- **No web queries from transcripts.** Transcript text is personal data from
  non-account participants. `xiu.3.2` query minimization must treat it as never
  externalizable.

## 5. GM and player interfaces

**Current state.**

- **Channels.** A four-channel workspace. The GM channel is shown to `dm`
  accounts (`ui/src/shell/modes.ts:46`) and enforced server-side.
- **Rendering.** `ChatPane` renders the user's prompt as a player bubble and the
  answer as a DM bubble.
- **Navigation.** No router, deep links, player shell, or campaign context.
- **Stale documentation.**
  - README says the GM channel is "a profile toggle — UI gating until real auth
    exists".
  - `modes.ts:44-45` says server enforcement "waits for real auth".
  - Both predate server enforcement (`ui/src/shell/currentUser.tsx:21-24`).

**Gaps.**

- **GM live surface.** Listening banner and controls, a live card rail/feed,
  transcript panel, correction dialogs, surfacing-policy settings, disclosure
  log, and recap review.
- **Player surface.** Consent prompt, capture indicator, pause-for-everyone,
  and withdrawal inside the Workbench table client (`1kg.7.4`), which the MVP
  therefore depends on; player displays of live-card content come in Phase 4.
  Guests on the table link receive only GM-approved displays, never unreviewed
  AI output.
- **Layout.** A narrow/mobile layout for a GM running the table from a tablet or
  phone. The Workbench's 472 px chat floor and canvas assume a wide desktop.
- **Accessibility.** Live-region announcements for listening start/stop and card
  arrival; keyboard push-to-talk; reduced-motion indicator variants.

## 6. NPC images and entity matching

**Current state.**

- **Rules entities.** The corpus vocabulary matcher finds rules entities and
  classes (`ingestion/retrieval.py:128,308,479-500`). That covers monsters and
  spells from licensed books, but not campaign NPCs.
- **Documents.** Planned in `1kg.5.*`; the handoff NPC dossier has a portrait
  slot.
- **Media.** Planned in `1kg.8.1`.
- **UI.** `Avatar` renders an image URL; nothing authorizes image reads.

**Gaps.**

- **Entity index.** Campaign entity index over documents: kind, canonical name,
  aliases, pronunciation hints, scene/location context.
- **Resolution.** Exact, phonetic (Metaphone-style), trigram (`pg_trgm`), and
  embedding matching with calibrated thresholds and an ambiguity state.
- **Alias visibility (critical).** "The hooded stranger → Strahd" is itself a
  secret. The alias graph needs per-audience visibility and a *persona* pattern,
  so a public persona and a hidden identity are distinct player-facing entities.
- **Portraits.**
  - Audience-scoped derivatives with metadata stripped (EXIF, filename, alt text).
  - Opaque per-audience asset identifiers.
  - A warning when one image is reused across a persona and its hidden identity.
- **Vocabulary for STT.** Campaign entity names must feed speech-to-text
  keyterm/prompt hints. Proper-noun accuracy is the dominant quality risk.

## 7. Notifications and proactive cards

**Current state.**

- **Components.** Content cards exist with full/compact density.
  `AssistantLane` (planned `1kg.3.2`) defines working/done/error status and at
  most three suggestions.
- **Accessibility.** Live regions are limited to:
  - the auth gate (`ui/src/App.tsx:47` status; `:59` `role="alert"`);
  - the conversation title (`ui/src/shell/TopBar.tsx:28`,
    `aria-live="polite"`);
  - the sign-out error (`ui/src/shell/UserMenu.tsx:86`, `role="alert"`);
  - the empty-state and pending-answer status text in `ChatPane`
    (`ui/src/shell/ChatPane.tsx:308` and `:331`, `role="status"`; PR #57).

  Nothing announces proactive content.
- **Missing mechanics.** No notification queue, push channel, or unread state.

**Gaps.**

- **Card model.** Kind, priority, evidence state (confirmed / likely /
  suggestion / inferred), audience channel, entity references, trigger excerpt
  (GM only), and lifecycle (shown, pinned, dismissed, snoozed, corrected,
  shared, retracted, expired).
- **Ranking.** Trigger ranking, global and per-entity cooldowns, bundling, rate
  limits, a focus mode, and quiet periods after safety-tool use.
- **Correction.** A correction loop that feeds alias learning and suppresses
  repeats.
- **Channels.** Separate GM and player message schemas. A `PlayerCard` type
  structurally cannot carry sources, GM notes, or internal IDs.

## 8. Audit logging and privacy controls

**Current state.**

- **Metrics.** A bounded, allowlisted metrics catalog with fixed label sets
  (`service/metrics.py:21-93`).
- **Logging.** `gcp_logging.emit` writes structured Cloud Logging entries and
  cleans the extra fields passed to it: newlines stripped, 256-character caps
  (`service/gcp_logging.py:45-48`). Only throttle events use it
  (`service/app.py:396,659`). Everything else is ordinary, uncapped logging.
- **Log content.** Operational logs include conversation IDs and exception
  strings (`service/app.py:827-830`). Provider exception text is logged
  verbatim.
- **Deletion.** Account deletion cascades to conversations, messages, and
  attachments.
- **Missing controls.** No audit table, consent records, disclosure records,
  retention jobs, export, or conversation delete endpoint (`1ka.5`).
- **Tracing.** When enabled, tracing is content-bearing.

**Gaps.**

- **Audit log.** Append-only and content-free: actor, action, object IDs,
  decision, policy revision, payload hash. It must cover capture start/stop,
  consent changes, disclosures, retractions, classification changes, exports,
  and deletions.
- **Consent records.** Versioned: consent from each participant and guest
  device, age attestations, withdrawals, and one-time deletion codes (hashed).
  GM notice attestations with a roster count are kept alongside as evidence of
  notice; they are never consent.
- **Transcript encryption.** Application-level envelope encryption for
  transcripts and extracted events. Per-campaign data keys enable
  crypto-shredding, so deletion also covers database backups.
- **Retention.** Jobs for transcripts, clip temp objects, cards, and
  consent/audit records, each with its own schedule.
- **Log hygiene.** A logging denylist plus tests that fail when transcript or
  canary text reaches logs, traces, metrics, or error reports.
- **Children.** Age and sensitive-environment policy enforcement. Age is
  attested per consenting device at consent time, because participants and
  guests have no accounts; account signup (`yje.2.2`) does not supply it.

## 9. Current data model limitations

| Limitation | Evidence | Impact on the Live Session Assistant |
| --- | --- | --- |
| Client-generated `TEXT` conversation IDs; first writer owns | `04-chat-schema.sql`, `app.py:741` | Live windows must attach to server-issued campaign/session IDs, never client-minted ones |
| Global role only | `05-auth-schema.sql` | Cannot express co-GM, player-of-campaign, or per-campaign GM |
| No campaign aggregate | — | No boundary for consent policy, vocabulary, entities, retention, or budget |
| No characters/groups | Not in `1kg.2.1` scope | "Specific character" and "selected group" audiences need new tables |
| Messages store role/content/mode/suggestions only | `04-chat-schema.sql` | Cards, evidence, disclosures, and corrections need typed tables, not chat rows |
| Attachments store extracted text only | `chat.attachments` | Cannot hold audio; must not become the transcript store |
| Idempotent startup SQL; per-operation connections | `service/history.py`, `1kg` plan | Multi-table transactional writes (window + segments + audit + outbox) need `1kg.1.5` |
| No object storage | — | Upload mode and portraits depend on `1kg.8.1` |
| No KMS integration | — | Crypto-shredding requires Cloud KMS wiring |
| Stateless, non-revocable GM sessions (14-day TTL) | `session.py`, `config.py:167` | GM account compromise exposes GM data. `yje.2.3` is recommended before broad Phase 4 rollout. Participants use Workbench device credentials, which a reset personal link revokes (AUD-5). |
| Count-based usage caps | `config.py:230,235` | Audio minutes cost 10–100× a chat turn; needs cost-ledger reservations (`yje.5.*`) |

## 10. Handoff and plan gaps specific to live sessions

1. **No capture UX at all.** Every listening screen, indicator, and consent
   state needs original design. The existing AudioCue/AudioConsentPrompt
   visual language may be reused, but not its semantics.
2. **Per-player delivery exists only as display slots.** The decision record's
   participant slots, authenticated by enrolled devices, give live-card shares a
   per-recipient channel. Nothing in the record decides *which* fields may go to
   which slot; that is eligibility (`LSA-1.2`).
3. **"Stop showing" is not "un-know".** Stop narrows the display immediately.
   Without a disclosure ledger, nobody can audit what was shown, and automation
   could not know that a field was deliberately stopped.
4. **Assistant document edits and pinned versions.** The record's pinned-version
   rule (REVEAL-8) already prevents AI edits from changing what players see. This
   plan inherits it.
5. **Recap tool.** `1kg.4.4` dispatches recap generation into documents. A
   player-safe recap must be generated from player-authorized inputs or
   GM-approved text. It must never be produced by redacting a GM recap.

## Reuse versus build

| Reuse | Build |
| --- | --- |
| `require_session` re-read + fail-closed lookups | Campaign-scoped principal resolver with authz revisions |
| Ownership/role adversarial test patterns | Canary-seeded player-path leak harness and policy oracle |
| `ProviderClientFactory` / catalog posture | STT adapter family and extraction-model qualification |
| Bounded metrics catalog, `record_safely` | Live metrics without content labels; audit log (separate from metrics) |
| `RagRetriever` + mode scoping for rules/spells | Authorization-bound campaign retriever, field-granular chunking, DB roles/RLS |
| Corpus vocabulary matcher | Campaign entity index, aliases, phonetic/fuzzy resolution |
| `GameContentCard`/`SpellCard`/`StatBlockCard` | Card lifecycle, ranking, cooldowns, GM/player card schemas |
| Planned `AssistantLane` (`1kg.3.2`) | Live card rail, listening banner, transcript review |
| Planned campaigns/sessions/documents/media/reveal/realtime (`1kg`) | Consent/attestation, capture windows, disclosure ledger, knowledge grants |
| Planned cost ledger/entitlements (`yje`) | Audio-minute and session-hour allowances, live kill switches |
| Planned evidence contract (`xiu.1.2`) | Visibility/audience/version fields on evidence |

## Recommendation

1. **Settle one permission model now.** Extend the Workbench display model with
   eligibility classes and a disclosure ledger (`LSA-1.2`). Confirm it together
   with escalations E-1, E-2, E-3, E-6, and E-10, and decide the requested
   amendments to AUD-9, NG-20, NG-22, NG-25, X-2, and the one-slot invariant
   (master plan §2.5), before `1kg.2.1`, `1kg.5.1`, `1kg.5.3`, or `1kg.7.1`
   freeze schemas.
2. **Fix `va8` before any model-written card renders.**
3. **Ship push-to-talk first, inside a table session.** A bounded HTTPS clip
   upload fits the current Cloud Run request model and delivers GM-only value. It
   still exercises per-participant consent, indicators, STT, entity resolution,
   authorization-bound retrieval, audit, retention, and cost metering.
4. **Keep player displays last.** Player-facing output stays out of scope until
   eligibility enforcement, slot delivery, per-recipient egress filtering, the
   adversarial suite, and an external review are complete.
