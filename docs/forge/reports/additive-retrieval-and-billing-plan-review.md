# Plan Review: additive retrieval and subscription billing

Sources: `docs/forge/plans/additive-retrieval-web-fallback.md`,
`docs/forge/plans/subscription-billing-coupons-profitability.md`, and their
integration into `docs/forge/plans/aetheril-gm-workbench-expansion.md` ·
Reviewed: 2026-09-16

Review scope: claims about current code (verified against `master` at
`042b198`), external pricing and licensing facts (verified against live
first-party pages on 2026-09-16), the unit-economics math, and the live Beads
graph for epics `agent-forge-harness-xiu` and `agent-forge-harness-yje` (67
beads at review time).

## Verdict: NEEDS REVISION — 0 Blocker / 5 High / 8 Medium / 4 Low

The direction is sound, most factual claims hold, and the Beads graph was
structurally valid. The findings are sequencing contradictions, a
content-rights gap, a coupon design that Stripe does not support as described,
pilot controls that cannot survive a paid launch, and premises about current
code that are out of date. Both plans and the Beads graph were revised on the
same day; see "Changes applied" and "Follow-ups".

## What held up

- **Beads:** 67 beads (2 epics, 11 features, 7 decisions, 47 tasks), each with a
  description, acceptance criteria, and a spec link; `bd lint` clean; no
  dependency cycles.
- **Math:** every price-table row, the break-even counts (10, 23, 46), the
  $0.00126 baseline turn, the ~$1.50 variable-cost estimate, and the $150 coupon
  exposure reproduce.
- **Code:** the refusal gate (`service/graph.py:198-204`), `REFUSAL`
  (`service/models.py:135`), the 503 mapping (`service/app.py:845-859`), the
  20/hour and 500/day limits (`config.py:227-235`), atomic invite signup,
  14-day stateless cookies without per-session revocation (`service/session.py`,
  `config.py:167`), no outbound email, `gpt-4o-mini` and
  `text-embedding-3-small`, and the ~$9.40/month database under a $10 kill switch.
- **External facts:** gpt-4o-mini at $0.15/$0.60 per million tokens,
  text-embedding-3-small at $0.02; Stripe cards 2.9% + $0.30, Billing 0.7%, Tax
  0.5%; SRD 5.1 and 5.2 under CC BY 4.0; Stripe's advice to persist entitlements
  locally.

## Findings

| # | Severity | Finding | Evidence | Resolution |
|---|---|---|---|---|
| 1 | High | The content-rights boundary misses the wikidot corpus and web fallback. | `docs/licensing-wikidot-corpus.md:31-36` scopes wikidot to the closed tier but floats it for a public tier; 422 of 849 wikidot chunks overlap book text (`docs/forge/reports/dnd-corpus-wikidot-expansion-ship.md:56`); the site hosts full non-SRD entries and Wikidot's terms make contributors responsible for posted material; no SRD text is ingested; the only corpus filter is `book_slug` (`ingestion/retrieval.py:214-216`), used only by Spell mode; PDF chunks store no license (`ingestion/embed.py:145-147`); no xiu bead had a licensing rule. | `yje.1.1` scope expanded; new `yje.4.6` (SRD ingestion and entitlement filter, blocks `yje.6.4`); `xiu.1.3` and `xiu.3.3` AC; `xiu.5.5` blocked by `yje.1.1`; billing plan boundary section rewritten. |
| 2 | High | Pilot controls cannot survive a paid launch. | The daily cap counts every user's turns (`service/history.py:210-212`); the hourly limiter is per instance in memory (`service/ratelimit.py:21-23`, `scripts/deploy.sh:153`); the cap is skipped when the message store failed at startup (`service/app.py:198-207`, `:691-692`); the kill switch detaches billing for the whole project (`scripts/gcp/billing_killswitch/main.py:3-6`) and the database uses ~94% of it (`docs/deploy-gcp.md:721`); ingress is locked by `scripts/deploy.sh:12-17` and `tests/test_deploy_contract.py:155`. `yje.5.2` kept the pilot guards "as defense in depth". | `yje.5.2` AC replaces the guards; new `yje.6.6` (kill switch) and `yje.6.7` (public ingress), both blocking `yje.6.4`. |
| 3 | High | The graph could not ship the plan's Phase 1, and the blocked piece was safety-critical. | `xiu.5.1` was blocked by `xiu.3.3`, `xiu.5.3` by `xiu.3.4`, and `xiu.5.5` by `xiu.4.5`, which required P2 `xiu.4.2` and `xiu.4.4`. The UI hides sources and shows no qualification for `answerable=false` outside GM (`ui/src/shell/ChatPane.tsx:291-322`), and history reload fabricates `answerable: true` (`ui/src/useChat.ts:76-81`). | New `xiu.5.6` (Phase 1 rollout); `xiu.5.5` scoped to web and blocked by `xiu.5.6`; blockers removed as listed below; `xiu.4.3` raised to P1. |
| 4 | High | "Sponsored access, no card" cannot be built on a 100%-off repeating coupon. | When the discount ends Stripe invoices normally, and a subscription without a payment method goes `past_due` into account-wide dunning; trials with `trial_settings.end_behavior.missing_payment_method=cancel` are the documented pattern. Per-code expiry is the promotion code's `expires_at` (`redeem_by` is the coupon field); Stripe allows up to 20 discounts per subscription; API `2025-09-30.clover` moved codes to `promotion[coupon]`. | `yje.3.5` rewritten (local grant or trial for no-card; codes only for card promotions); `yje.3.6` AC; `yje.1.4` inputs; billing plan coupon section. |
| 5 | High | Nothing measures real cost, yet the P0 pricing decision assumed it. | The graph calls generation without an `AttemptObserver` (`service/graph.py:226-229`), so `NullAttemptObserver` discards every attempt (`service/generate.py:124`); a turn can make up to three generation attempts (`service/generate.py:107`); the model picker labels responses without passing the model (`service/app.py:790-793`); `yje.5.1` was blocked by `yje.1.2`. | `yje.5.1` owns production usage capture and is now unblocked; `yje.1.2` AC requires a real usage baseline; `xiu.3.4` blocked by `yje.5.1`. |
| 6 | Medium | Two retrieval-plan premises about current code were stale. | History writes and attachment fetches already swallow their own errors (`service/app.py:485-518`); embedding API errors surface through the generation-error branch as 429/502 (`service/app.py:93`, `:822`) and only a missing key yields 503 (`:852-859`); one `DATABASE_URL` and one `OPENAI_API_KEY` serve corpus, auth, and chat; attachments already bypass the gate (`service/graph.py:192-197`). | `xiu.2.1` AC corrected; `xiu.2.3` rescoped; plan "Why" and failure-contract sections corrected. |
| 7 | Medium | The plan reverses the documented grounded-only promise, and its decision bead pre-decided the outcome. | `docs/ARCHITECTURE.md:5-7`; the answerability gate in `docs/forge/plans/dnd-agent-service.md`; `xiu.1.1` AC required that no corpus miss ever refuses. | `xiu.1.1` AC outcome-neutral with a refusal-rate baseline from `service.chat.gate.answerable`; `xiu.2.2` AC follows the decision behind a flag; plan section added. |
| 8 | Medium | The master-plan integration was shallow. | Only two coordination rows were added. "`/rules` retains current grounding/citation rules" (Quality and release gates, AI quality and cost) is in tension with the `xiu` row of "Existing work to coordinate rather than duplicate", which says Workbench tools use the xiu evidence contract, and that row's claims that Workbench routes consume yje entitlements and the evidence contract have no bead edges. Correction to the initial review: invariant 9 only requires legacy chat and auth to keep working during Workbench rollout and is not contradicted. | Not applied to the master plan, which is on PR #58; follow-up bead `yik` and a coordination comment on `1kg`. |
| 9 | Medium | Overlapping ownership was linked only by non-blocking edges. | Migration tooling: `1kg.1.5`, `yje.2.1`, and implicitly `xiu.5.2`. Lossless history: `xiu.5.2` and `1kg.4.2`, unlinked. Cost accounting: `yje.5.1`, `xiu.3.4`, `xiu.5.3`, `1kg.9.2`, `b8o.5`. | `yje.2.1` and new `yje.3.7` blocked by `1kg.1.5`; `xiu.5.2` owns the evidence payload and reuses the runner (relates to `1kg.4.2`); `yje.5.1` is the single ledger (`xiu.3.4` blocked by it; relates to `1kg.9.2`). |
| 10 | Medium | Priorities did not follow the harness rubric. | 3 P0 and 60 P1 of 67; P0 `yje.6.1` blocked by P1 `yje.1.4`; P1 `xiu.4.5` blocked by P2 `xiu.4.2`–`xiu.4.4`. The rubric reserves P0 for production down or data loss. | P0 to P1 on `yje.1.1`, `yje.1.2`, `yje.6.1`; inversions removed. Remaining P1s kept: the owner confirmed all of this ships before public launch, which the rubric treats as a release blocker. |
| 11 | Medium | Identity work and cost caps were coupled to the payment provider. | `yje.2.1` (and so `yje.2.2`–`yje.2.4`) was blocked by the billing ADR `yje.1.4`; `yje.5.2` sat behind the Stripe projection chain via `yje.4.1`; no bead considered a managed identity provider; signup already enumerates accounts (`service/app.py:1019-1023`, `service/invites.py:82-87`). | New decision `yje.1.5`; `yje.2.1` identity-only and no longer blocked by `yje.1.4`; new `yje.3.7` holds billing schema; `yje.5.2` decoupled from `yje.4.1`; `yje.2.2` AC. |
| 12 | Medium | The merchant-of-record alternative was listed but never costed. | Stripe Managed Payments acts as merchant of record for 3.5% on top of standard processing; Paddle charges 5% + $0.50. At $15 and $3 COGS both stay above 70% contribution. | `yje.1.4` inputs and AC; `yje.1.2` AC; sensitivity table in the billing plan. |
| 13 | Medium | $0.015 per web turn holds for only one configuration. | `web_search` is $10 per 1k calls plus a fixed 8,000-input-token block for mini models; `web_search_preview` is $25 per 1k on non-reasoning models; Chat Completions search models shut down 2026-07-23; generation uses Chat Completions (`service/providers.py:73-90`); the catalog prepares non-OpenAI models (`service/model_catalog.py:55-72`). | `xiu.1.4` inputs and AC (`max_tool_calls=1`, cost statement, separate acquisition adapter recommended); retrieval plan cost section. |
| 14 | Low | The web-source JSON example fails the current UI schema. | `SourceSchema` requires `book` and present-but-nullable fields (`ui/src/schemas.ts:10-17`). | `xiu.1.2` AC; retrieval plan API section. |
| 15 | Low | "$15 is the only scenario preserving roughly 70%" was imprecise. | $12 reaches 68.4%; $15 is the only scenario above 70%. | Billing plan wording corrected. |
| 16 | Low | Existing licensing precedent was not cited. | Decision `x5bz.5`; standing rule at `docs/invite-copy.md:35-38`. | Billing plan boundary section; `yje.1.1`. |
| 17 | Low | Nothing was committed or backed up. | The plan files were untracked in git. The repo Beads tracker exists only on this machine: `.beads/` is gitignored, `bd backup` writes inside the checkout, and the auto-detected Dolt remote (the public GitHub repository) has never been pushed. | Plans and this report published by PR; tracker destination decision `m51`. |

## Verified external facts (checked 2026-09-16)

| Fact | Value | Source |
|---|---|---|
| OpenAI `web_search` | $10 per 1k calls plus content tokens; gpt-4o-mini and gpt-4.1-mini pay a fixed 8,000-input-token block per call | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| OpenAI `web_search_preview` | $25 per 1k calls on non-reasoning models, content tokens free | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| Per-turn web cost (6,000 in / 600 out, gpt-4o-mini) | ~$0.0125 with one search; ~$0.024 with two; ~$0.026 on the preview tool | Derived from list prices |
| Chat Completions search models | Shut down 2026-07-23; gpt-4o-mini itself not deprecated | [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations) |
| Responses API controls | `max_tool_calls`, `search_context_size`, `filters.allowed_domains`; inline citations must be visible and clickable | [Web search guide](https://developers.openai.com/api/docs/guides/tools-web-search) |
| Stripe fees | Cards 2.9% + $0.30 (+1.5% international, +1% conversion); Billing 0.7%; Tax 0.5% no-code or $0.50 per API transaction | [Stripe pricing](https://stripe.com/pricing) |
| Stripe coupons and codes | `duration=repeating` with `duration_in_months`; coupon `redeem_by`; code `expires_at`, `max_redemptions`, customer restriction; up to 20 discounts per subscription; `promotion[coupon]` from 2025-09-30.clover | [Coupons and promotion codes](https://docs.stripe.com/billing/subscriptions/coupons) |
| No-card free period | Trial with `payment_method_collection=if_required` and `missing_payment_method=cancel` | [Free trials](https://docs.stripe.com/billing/subscriptions/trials/free-trials) |
| Stripe Managed Payments | Merchant of record handling sales tax, VAT, and GST; 3.5% on top of standard processing; works with Checkout, Payment Links, and Billing | [Managed Payments](https://stripe.com/managed-payments) |
| Paddle | 5% + $0.50 per transaction | [Paddle pricing](https://www.paddle.com/pricing) |
| SRD licensing | SRD 5.1 under CC BY 4.0 (or OGL 1.0a); SRD 5.2 under CC BY 4.0, covering the 2024 rules | [D&D Beyond SRD](https://www.dndbeyond.com/srd) |
| Wikidot | Site footer declares CC BY-SA 3.0 unless otherwise stated; terms make contributors responsible for posted material | [Wikidot terms](http://www.wikidot.com/legal:terms-of-service) |

## Changes applied

### Beads

- **New beads (6):** `xiu.5.6` (Phase 1 rollout), `yje.1.5` (identity
  implementation decision), `yje.3.7` (billing schema migrations), `yje.4.6`
  (SRD ingestion and corpus-entitlement enforcement), `yje.6.6` (replace the
  project-wide kill switch), `yje.6.7` (open and harden public ingress).
- **Retitled or rescoped:** `xiu.2.3`, `xiu.4.5`, `xiu.5.3`, `xiu.5.5`,
  `yje.2.1`, `yje.3.5`.
- **Acceptance criteria updated:** `xiu.1.1`, `xiu.1.2`, `xiu.1.3`, `xiu.1.4`,
  `xiu.2.1`, `xiu.2.2`, `xiu.3.3`, `xiu.5.1`, `yje.1.1`, `yje.1.2`, `yje.1.4`,
  `yje.2.2`, `yje.3.5`, `yje.3.6`, `yje.5.2`.
- **Blocking edges removed:** `xiu.5.1` ← `xiu.3.3`; `xiu.5.3` ← `xiu.3.4`;
  `xiu.4.5` ← `xiu.4.2`, `xiu.4.4`; `yje.5.2` ← `yje.4.1`; `yje.5.1` ←
  `yje.1.2`; `yje.2.1` ← `yje.1.4`; `yje.3.2`, `yje.3.3`, `yje.3.4` ← `yje.2.1`.
- **Blocking edges added:** `xiu.5.6` ← `xiu.2.2`, `xiu.2.3`, `xiu.2.4`,
  `xiu.4.1`, `xiu.5.1`, `xiu.5.2`, `xiu.5.3`; `xiu.5.5` ← `xiu.5.6`, `yje.1.1`;
  `xiu.5.3` ← `xiu.2.2`; `xiu.3.4` ← `yje.5.1`; `yje.4.6` ← `yje.1.1`;
  `yje.6.4` ← `yje.4.6`, `yje.6.6`, `yje.6.7`; `yje.5.3` ← `yje.4.1`; `yje.2.1`
  ← `1kg.1.5` (replacing a `relates-to`), `yje.1.5`; `yje.3.7` ← `yje.1.3`,
  `yje.1.4`, `1kg.1.5`; `yje.3.2`, `yje.3.3`, `yje.3.4`, `yje.4.1` ← `yje.3.7`;
  `yje.6.6` ← `yje.5.5`; `yje.6.7` ← `yje.2.2`, `yje.2.3`, `yje.4.6`.
- **Relations added:** `xiu.5.2` ↔ `1kg.4.2`; `yje.5.1` ↔ `1kg.9.2`.
- **Priorities:** `yje.1.1`, `yje.1.2`, `yje.6.1` from P0 to P1; `xiu.4.3` from
  P2 to P1.
- **Audit trail:** every changed bead has an `ac:`, `deps:`, `design:`, or
  `worklog:` comment, and both epics carry a `review:` comment.

Result: 73 beads (xiu 31, yje 42: 2 epics, 11 features, 8 decisions, 52 tasks).
Verified after the change: no dependency cycles, no priority inversions within
xiu and yje, `bd lint` clean, and no Workbench (`1kg`) blocking edge, priority,
or status changed. Unblocked now: `xiu.1.1`, `xiu.2.1`, `yje.1.1`, `yje.1.2`,
`yje.1.5`, `yje.5.1`.

### Plans

- `additive-retrieval-web-fallback.md`: corrected the current-state and
  failure-contract premises, added the grounded-only supersession note, the
  content-rights source policy, UI schema compatibility, verified search pricing
  and integration constraints, the revised phases, the bead map, coordination,
  and sources.
- `subscription-billing-coupons-profitability.md`: rewrote the content-rights
  boundary (wikidot, web, model-only, SRD enforcement), expanded current state
  (enumeration, sessions, router, ingress lock, shared cap, per-instance limits,
  missing usage capture, project-wide kill switch), added merchant-of-record
  costing and fee sensitivity, fixed the coupon design and API details, grounded
  the usage baseline, and updated the identity migration, profitability rules,
  phases, bead map, coordination, go-live definition, and sources.

- `docs/licensing-wikidot-corpus.md`: added a dated review note that its
  public-tier suggestion and re-serve conclusion should not be relied on for any
  audience wider than the closed pilot until `yje.1.1` is decided.

## Follow-ups

Work the review identified but could not complete here is tracked in Beads:

| Bead | Priority | Follow-up |
|---|---|---|
| `agent-forge-harness-yik` | P2 | Align the GM Workbench master plan and beads with these initiatives. The master plan is on PR #58, whose branch holds a newer copy than the one reviewed, so the review did not edit it. Covers the `/rules` grounding tension and edges such as `1kg.9.6` blocked by `yje.4.1` and the rules card tool in `1kg.4.3` using `xiu.1.2`. |
| `agent-forge-harness-m51` | P1 | Decide where the Beads tracker syncs. The auto-detected Dolt remote is the public GitHub repository and has never been pushed; the session-completion step `bd dolt push` would publish the issue graph there. Do not push this tracker until the decision is made. |
| `agent-forge-harness-88v` | P2 | Clear stale untracked documents from the shared main checkout, including local copies of this change's files, so it can fast-forward after the related PRs merge. |

Workbench beads: no `1kg` bead's own dependencies, priority, or status changed.
`1kg.1.5` gained two dependents (`yje.2.1`, `yje.3.7`), `1kg.4.2` and `1kg.9.2`
gained relations, and `1kg` gained a coordination comment.
