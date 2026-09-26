# Research: Jev (TypeSafe AI) as a real-time decision layer for game-guide-ai

Date: 2026-09-24
Bead: `agent-forge-harness-9z8` (research spike; documents only)
Code base: `integration/1kg-workbench` at `2770766`; every `path:line` below is at that commit
Status: research. No request was sent to TypeSafe's API, no account was created, and no access was
requested. Vendor pages were read on 2026-09-24 as public web pages.

**Provenance tags**

| Tag | Meaning |
| --- | --- |
| **[REQ]** | Supplied requirement: the owner's brief of 2026-09-24, bead `9z8`, or a recorded owner decision |
| **[REPO]** | Repository fact, cited `path:line` |
| **[VENDOR]** | TypeSafe's own claim, cited by URL. Not independently verified |
| **[IND]** | Independent third-party evidence, cited by URL |
| **[INF]** | My inference or estimate. Every estimate shows its formula so a measured number can replace it |

---

## 0. Verdict

1. **Jev can gate a call, but it cannot write anything.** Its three answer types return an option
   key, a level, or a probability [VENDOR, api.md]. It cannot fill a stat block, a spell card,
   the three spell suggestions or a Live Session Assistant extraction. Every structuring call in
   the service stays. At most, Jev decides whether to make one.
2. **Neither of the owner's two example uses pays for itself at today's scale** [INF]:
   - *Routing:* only `gpt-4o-mini` is enabled, and it is already the economy tier
     (`service/model_catalog.py:49-75`). `auto` resolves to it (`service/app.py:943-950`), so
     there is no cheaper model to route to until `b8o.3` qualifies one. The routing plan has
     already settled that routing "must not add a separate router-model call"
     (`docs/forge/plans/game-guide-ai-model-routing.md:33`, `:733-734`). Even with a cheaper
     model qualified, the most a routing decision can save is $0.72 per 1,000 turns moved to a
     $0.54-per-1,000 model (the plan's list prices, `:224-247`).
   - *Block choice:* a free heuristic already gates the stat-block call
     (`service/generate.py:458-469`). One avoided structuring call is worth about $0.00046, so a
     Jev veto costing about $0.000042 per check only pays if at least 9% of the calls it inspects
     are wasted. That waste rate is not measured yet, and measuring it does not need a vendor
     (§5.3).
3. **The decision where Jev would save the most does not exist yet.** It is the web-fallback
   trigger (`xiu`): one unnecessary search avoided saves about $0.0112, the price of about 330 Jev
   decisions. A mode rule plus a classifier over the query embedding the service already
   computes costs nothing, though, so Jev has to beat that, not a large LLM.
4. **The data terms rule out sending user text today.** TypeSafe commits in writing not to train
   on customer data. Its public terms bound neither how long it keeps that data nor what it
   derives from it. The Master Customer Agreement lets it keep or delete Customer Data "at any
   time in its sole discretion", and lets it use Customer Data "in perpetuity" to derive
   Telemetry, which it defines to include "classifications … and learnings". Zero data retention
   is offered only to Enterprise customers, on terms that are not public. This fails SEC-39's
   written "bounded retention" test for Workbench text. For `/chat`, the routing plan's own
   rules would limit TypeSafe to synthetic data (§4).
5. **Recommendation** (§6):
   - Put Jev on no path that carries user text.
   - **Step 0 (no vendor):** measure how often the stat-block and spell calls are wasted.
   - **Pilot 1 (offline; synthetic and CC-licensed data only):** choosing the content block,
     comparing four options.
   - **Pilot 2 (offline, after `xiu.1.4` exists):** deciding when the web fallback may run.
   - Reconsider shadow mode only after TypeSafe gives written zero-retention terms and a pilot
     passes.

---

## 1. What Jev is, precisely

### 1.1 Answer shapes, and what they cannot do

| Question type | Returns | Limits | Confidence | Source |
| --- | --- | --- | --- | --- |
| `choice` | One option key, plus `probabilities` over all options (they sum to 1) | Up to **255 options**. Single selection only | `confidence`: "TypeSafe computes confidence from how the probability is spread across the options". It is not the maximum probability | [VENDOR] [api.md](https://docs.typesafe.ai/api.md), [choice.md](https://docs.typesafe.ai/primitives/choice.md), [confidence.md](https://docs.typesafe.ai/confidence.md) |
| `score` | One level, plus `legend` and `probabilities` | **2 to 10 levels** ("API accepts up to 10") | Same as `choice` | [VENDOR] api.md |
| `noul` | A probability from 0 to 1 | None stated | **None** ("Noul answers don't carry one") | [VENDOR] api.md, confidence.md |

- All three types can be mixed in one call. Each question is "evaluated in parallel and in
  isolation" [VENDOR, [docs.typesafe.ai](https://docs.typesafe.ai/)].
- **No free text, spans, numbers, lists or objects are returned.** "Structure" applies only to
  how questions are written [VENDOR, [advanced.md](https://docs.typesafe.ai/primitives/advanced.md)].
  jev-1.13 "is not trained to generate text"
  [VENDOR, [jev-1.13.md](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)].
- **In TypeSafe's own extraction recipe, an LLM extracts and Jev only checks.** The recipe says:
  "Extract with a cheap/small model. Verify with TypeSafe primitives"
  [VENDOR, [sde_cascade.md](https://docs.typesafe.ai/cookbooks/sde_cascade.md)].
- **Consequence [INF]:** Jev can *gate* or *verify* a structuring call, but it cannot *replace*
  one. It can pick one ID from at most 255 known candidates, such as a campaign entity. It cannot
  produce a new NPC name, a hit-point value or a span of text.

### 1.2 Limits, latency, price, status, SDKs

| Item | Fact | Kind / source |
| --- | --- | --- |
| Endpoint | `POST https://api.typesafe.ai/v1/systemone` with a Bearer key. The body has `state`, `model` and `questions`. The response has `answers` and `usage` (`input_tokens`, `output_tokens`) | [VENDOR] [api.md](https://docs.typesafe.ai/api.md) |
| Errors | 401, 422, 429 Too Many Requests, 529 Overloaded | [VENDOR] api.md |
| Model | `jev-1.13.0`, aliased as `jev-latest`. "If you have tuned confidence thresholds against a specific version, pin that version's ID instead of the alias" | [VENDOR] [models.md](https://docs.typesafe.ai/models.md) |
| Input | "Text only. String, JSON object, or array of text values." Best in English; other languages "not equally well" | [VENDOR] models.md |
| Context | "64k tokens per request; 32k tokens for `state` plus the longest question" | [VENDOR] models.md |
| Price | **$0.042 per 1M input tokens** ($42 per 1B). Output tokens are free. The press figure is confirmed. No page says whether this is an early-access price | [VENDOR] models.md; [blog](https://typesafe.ai/blog/introducing-system-one-models-and-jev) |
| Rate limits | "250,000 tokens per second / 1,200 requests per minute". Limits "can change without notice". "Higher limits are available on custom and enterprise plans" | [VENDOR] models.md |
| Latency (claimed) | "End-to-end response time is 70ms-500ms" (blog). 0.114 s in the homepage demo | [VENDOR] blog; [homepage](https://typesafe.ai/) |
| Status | "available today in early access", "bringing developers off the waitlist". The brief gives 2026-09-15 as the start [REQ]. The contract has no beta or early-access clause | [VENDOR] blog; [MCA](https://typesafe.ai/legal/mca) |
| SLA | **None.** Support is "commercially reasonable efforts". Changes "may result in the API's becoming incompatible with a Customer Application" | [VENDOR] MCA |
| SDKs | Python `typesafe-sdk` (Python 3.10 or later) and a JavaScript SDK. The key is read from `TYPESAFE_API_KEY`. The Python default timeout is 10.0 s | [VENDOR] [quickstart](https://docs.typesafe.ai/introduction/quickstart.md), [constants](https://docs.typesafe.ai/sdk/python/api/constants.md) |
| SDK maturity | First public release v0.5.7 on 2026-09-14. v0.7.1 on 2026-09-21. **Two breaking changes in its first week** (v0.6.0, v0.7.0) | [VENDOR] [changelog](https://docs.typesafe.ai/sdk/python/changelog.md) |
| Known weaknesses of jev-1.13 | "not a calculator"; "does not count reliably"; "score levels are weak in numerical calibration"; reads dates as text; weak on indirection and double negatives; "Accuracy falls as the state grows with content unrelated to the decision"; "answers the question you wrote, not the one you meant"; "Content written to adversarially steer the model" can influence outputs; does not treat state "as hostile by default" | [VENDOR] [jev-1.13.md](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md) |
| Calibration evidence published by the vendor | **None in the docs:** no ECE, no Brier score, no reliability diagram. The vendor's workflow evals report "67.8% · $0.0004 · 0.4 s" (accuracy, cost, time) pooled across four business workflows. They score against labels averaged from two frontier models, and publish neither the datasets nor the code | [VENDOR] confidence.md; [evals.typesafe.ai](https://evals.typesafe.ai/) |

### 1.3 Data terms (critical)

| Question | Public answer | Source |
| --- | --- | --- |
| Training on our data | **No.** "We will not train or fine tune any artificial intelligence or machine learning models on your prompts or other Input." The MCA adds: not "without Customer's prior consent". "Jev is not trained on customer requests or responses." | [Privacy Policy](https://typesafe.ai/legal/privacy-policy) (updated 2025-11-19); [MCA](https://typesafe.ai/legal/mca) (updated 2026-09-23); models.md |
| Retention period | **None stated.** MCA: "no obligation to store or retain Customer Data and may delete Customer Data at any time in its sole discretion." DPA: "retained for as long as necessary taking into account the purpose of the Processing". Privacy Policy: "as long as reasonably necessary … or otherwise in support of our business or commercial purposes." | MCA; [DPA](https://typesafe.ai/legal/data-processing) (updated 2026-04-24); Privacy Policy |
| Derived use | The MCA allows use "in perpetuity" of any Customer Data "to derive and generate Telemetry" and "to monitor for fraud and abuse". Telemetry means "technical logs, hashes, summary statistics and classifications, metrics, and learnings related to Customer's use of the Services." | MCA |
| Zero data retention | "zero data retention (ZDR) for enterprise customers". **The terms and price are not public** | [legal.md](https://docs.typesafe.ai/legal.md); models.md |
| Subprocessors | The DPA points to `trust.typesafe.ai/subprocessors`, with advance notice and a 15-day objection window. That page **showed no list** when fetched (it probably needs JavaScript or is gated), so the list is **not public in practice**. The MCA allows subcontractors | DPA; [trust center](https://trust.typesafe.ai/subprocessors) |
| Region | "The Services are hosted in the United States." EU transfers use SCC Module 2 plus the UK Addendum | Privacy Policy; DPA |
| Certifications | **Not public.** No SOC 2 or ISO on any page read; the trust center showed no content | [trust.typesafe.ai](https://trust.typesafe.ai/) |
| Breach notice | "within 72 hours after becoming aware" | DPA |
| How the terms are accepted | By clicking, by an Order, or by "using (or making any payment for) any Services". **The first API call accepts the MCA.** The DPA is "incorporated herein by reference". Updates take effect at least 60 days after notice | MCA |
| Liability cap | The greater of 12 months' fees or $50 | MCA |
| Minors | The AUP bars services "intended to encourage, compulsive or excessive use by minors". Nothing addresses minors' data | [AUP](https://typesafe.ai/legal/acceptable-use-policy) (updated 2026-09-23) |

The site Terms of Use ([terms](https://typesafe.ai/legal/terms), 2026-09-19) cover the website,
not the API. The MCA governs the API.

### 1.4 Independent evidence

| Source | What was measured | Result | Limits the authors state |
| --- | --- | --- | --- |
| [liteLLM benchmark](https://docs.litellm.ai/blog/jev-auto-router-benchmark), 2026-09-18, `jev-1.13.0` against `claude-haiku-4-5` | Routing requests to 4 tiers (SIMPLE / MEDIUM / COMPLEX / REASONING): 80 cases, each run 3 times, 240 calls | Matched the authored labels 95.00% vs 73.75%. p50 126.81 ms vs 688.40 ms. p95 231.16 ms vs 896.94 ms. $0.007707 vs $0.198534 for the 240 calls | Labels "authored … without independent review"; synthetic prompts; **"Downstream answer quality was not measured"**; the two classifiers agreed with each other on only 78.75% |
| [PriorBench](https://github.com/priorbench/jev), pre-registered, 2026-09-20, `typesafe/jev-1.13-20260917` via OpenRouter from Western Europe; 5,721 calls, $0.176, MIT-licensed raw data | Text classification and behaviour probes | 95.9% zero-shot on a 400-item set, vs 77.2% for keywords and 66.0% for TF-IDF with logistic regression. **"Accuracy above threshold is flat from 0.50 to 0.95, then jumps to 100 % at 0.99, covering 60.2 % of traffic."** **"It always answers"**: a cake recipe was labelled a technical issue at 0.94. **"Without an explicit 'none of these' option, 0 of 30 out-of-scope messages were flagged — at 0.99 confidence."** Option order moved results by up to 13 points. Wrong criteria text dropped accuracy to 16.7%. "~430 ms floor". Throughput saturates near 11 req/s. 26 predictions confirmed, 21 falsified | Custom benchmarks; one location, day and version; gateway latency cannot be separated; some ground truth disputed |

No independent evaluation was found on tabletop or D&D text. None measured calibration on a
held-out domain set, or cost and quality downstream of a routing decision [INF].
[Tom's Hardware](https://www.tomshardware.com/tech-industry/artificial-intelligence/typesafe-ais-jev-offers-an-alternative-to-llms-that-claims-to-be-193x-faster-and-445x-cheaper-system-one-type-model-is-bespoke-for-probabilistic-decision-making)
repeats the vendor's multipliers in its headline, and its article body could not be retrieved.
Several search results were SEO sites that are neither primary nor measurements, and none was
used.

### 1.5 What this means for this service [INF]

1. **Gate, not writer.** See §1.1.
2. **Only the top confidence band can be trusted, and only with an explicit `none` option.**
   Middle-band confidences should not drive graded thresholds without our own calibration. The
   Live Session Assistant's 0.6 to 0.85 trigger thresholds are exactly that kind of graded use.
3. **Pin `jev-1.13.0`, and recalibrate on every version change.** Fix the option order and
   review the criteria text: wrong criteria are "catastrophic" (PriorBench).
4. **Treat the state as hostile.** Jev itself does not. A user can steer a decision made on
   their prompt. Never let a Jev answer start work (SEC-32, §4).
5. **Plan for an early-access dependency with no SLA and limits that change without notice.** It
   has to fail open to today's behaviour (§4, G7).
6. **Price is the smallest issue.** A 1,000-token decision costs $0.000042.

---

## 2. Inventory: every decision point in the service today

**Paid calls per `/chat` turn today [REPO]:**

| Mode | Paid calls |
| --- | --- |
| `rules` | Embedding + answer |
| `sage`, `gm` | Embedding + answer, plus a stat block when the heuristic fires |
| `spell` | Embedding + answer + suggestions + spell card |
| Turn refused by the gate | Embedding only |
| Empty prompt | None |

- The answer and suggestions calls retry up to 3 attempts; the structuring calls make 1
  (`service/generate.py:120`, `:441`, `:534`).
- Five `purpose` values record these calls (`service/usage_capture.py:75-79`).

| # | Decision point | Mechanism | Paid calls it makes or avoids | What is known about quality | Failure mode | Jev fit |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | Structural routes | Empty prompt → `refuse` (`service/graph.py:120-122`); mode scope (`ingestion/scope.py:33-67`); GM fan-out (`graph.py:148-153`, stub secondary `:192-202`); route after generation (`graph.py:264-274`) | D1's empty-prompt refusal avoids every call. The other routes make no paid call | Exact. Mode is the user's choice | None | **None.** These are deterministic by design |
| D2 | Query hints: `retriever.analyze` | Vocabulary and keyword match for classes, entities and content types (`ingestion/retrieval.py:556-564`, `:295-302`, `:305-330`) via `graph.py:137-139`. A filtered search over-restricted past distance 0.42 retries unfiltered (`retrieval.py:340-349`, `:458-464`; `config.py:98`) | None (in-memory vocabulary; the unfiltered retry is a database query) | Can be measured now against the 160 `expected_content_type` labels in `ingestion/golden_set.json`. No standalone accuracy figure is recorded | A paraphrase goes unrecognised, leaving no type or the wrong filter. The unfiltered retry softens this | `choice` over about 10 content types. The query embedding is already computed |
| D3 | Rerank gate `should_rerank` | Skip when content types intersect {monster, spell, magic_item, condition, race_feature} (`ingestion/rerank.py:18-20`, `:25-34`; `graph.py:174-182`) | None: the reranker is a local CPU cross-encoder, "234ms/10pairs" (`rerank.py:22`) | Spike: Hit@1 74.7% → 80.7% on prose; worse on monsters (`rerank.py:4-8`) | A wrong content type makes the wrong rerank choice. **Inert by default:** `RAG_RERANK` defaults to 0 (`config.py:155`) and `Dockerfile.cloud:38` builds without the rerank extra. A checkout cannot show whether Cloud Run overrides this | **None now** |
| D4 | Answerability gate `gate_node` | koz: top-1 cosine distance at most 0.50 (`retrieval.py:352-359`, `config.py:104`). Sage, spell and rules need answerable *and* chunks. GM needs chunks. An attachment always generates (`graph.py:204-219`) | A refusal avoids the answer call, about $0.00126, or about $0.0025 in spell mode, which also skips suggestions and the card [INF, §3.2] | koz is scored on 5 hand-curated negatives (`ingestion/eval_golden.py:123-131`, `:306-309`). The live refusal rate by mode is `service.chat.gate.answerable` (`service/metrics.py:50`, emitted at `service/app.py:987`) | An answerable question is refused, or an answer is not grounded. Distance does not test whether the passages actually answer the question | `noul` "do these passages answer?", but Jev would have to see the question plus about 5,000 tokens of **licensed** passages (§4, G4) |
| D5 | Spell suggestions, always on in spell mode | `suggest_node` (`graph.py:276-297`) → `generate_suggestions` (`generate.py:357-375`) over the top-5 context | One call per spell turn, about $0.00089 [INF] | The three-style JSON contract is enforced (`generate.py:329-354`); a failure yields `None` (`graph.py:294-296`). Usefulness is not measured | The call is wasted when the answer is a non-answer or covers several spells | `noul` "is the answer about exactly one castable spell?", as a gate only |
| D6 | Spell card, always on in spell mode | `structure_node` spell branch (`graph.py:302-315`) → `generate_spell_content` (`generate.py:417-443`); `name` and `description` required (`:391-392`) | One call per spell turn, about $0.00033 [INF] | Validated against the schema; a failure yields `None`. **The outcome is not recorded anywhere.** Neither `stat_block` nor `spell_content` is persisted: no reference exists in `service/sql/migrations` or `service/conversation_store.py` | A call is wasted on non-answers. Wrong field values are something no gate can catch | Same gate as D5; one `noul` can serve both |
| D7 | Stat-block cost gate `_looks_like_statblock` | Needs at least 2 of: "armor class", "hit points", "challenge rating", `cr N`, `str`/`dex` followed by a space (`generate.py:453-469`), checked in `structure_node` (`graph.py:321-322`) → `generate_stat_block` (`generate.py:513-536`) | Sage and GM only. It skips the call on plain narrative; when it fires it adds one call, about $0.00046 [INF] | **No labelled set; precision and recall are unknown.** The firing rate becomes measurable from `statblock_structuring` records (`docs/runbooks/usage-capture.md:25-31`) | A false positive (prose that quotes AC and HP) wastes a call and returns no card. A false negative is an abbreviated block ("AC 15 · HP 45 · STR 10 …" with no CR line scores one marker), shown as prose without a card | `noul` or `choice` on the answer: a *veto* on heuristic positives, or a *recall booster* on negatives |
| D8 | Model choice | The user picks `auto` or an enabled alias (`service/app.py:925-941`). `auto` → `DEFAULT_ALIAS` until `b8o.4` (`app.py:943-950`). Only `gpt-4o-mini` is enabled (`model_catalog.py:49-75`). Generation still uses the service's single `svc.model` (`service/rag.py:74`; `graph.py:252-256`). Planned: a deterministic lookup / synthesis / creative classifier with **no router-model call** (routing plan `:33`, `:731-771`) | None | Not built. The `b8o.3` matrix will measure it | Planned failure: a misroute means a quality loss or overspend | `choice` over task class or tier. **This contradicts the settled design** |
| D9 | Credit-limit degrade (owner decision D-7) | "At 100% the turn completes on the cheapest model, premium features switch off, and an upsell is shown. Never a cut-off mid-answer." (`agent-forge-harness/.tmp/work/owner-decisions-2026-09-21.md:13`; in-repo `docs/forge/plans/subscription-billing-coupons-profitability.md:214`) | None. It is a counter against a credit number, and the numbers are still open (`:216-218`) | Not built | Exact once built | **None.** No model decision is needed. It must override any Jev routing |
| D10 | Web-fallback trigger (planned, `xiu`) | Eligibility has 7 conditions, including "below the evaluated sufficiency threshold", "the request is factual or asks for current/external information" and "the prompt can be converted to a public query without attachment, campaign, participant, or secret text" (`docs/forge/plans/additive-retrieval-web-fallback.md:166-181`). One search, a 2–3 s deadline (`:183-197`). "search must be conditioned on a miss" (`:332`) | A search is $0.010 plus a fixed block of 8,000 input tokens ≈ **$0.0112** on top of a $0.00126 turn (`:298-309`) [INF arithmetic] | Thresholds are to be calibrated (`xiu.4.3`). No labels | A false positive costs about 9 turns' worth and adds irrelevant web context. A false negative leaves a `model_only` answer | `choice` "factual or current vs creative vs other" on the prompt. It **must not** decide the privacy condition: to do so it would have to see the private text |
| D11 | Live Session Assistant: "worth a card, and which card?" (planned, `1ir`) | A free lexicon and question detector on every utterance. One small-LLM classifier call per 20 s micro-batch labels out-of-game chatter and personal data (triggers T18, T19). Small-LLM extraction runs at most once per 30 s, when the detector fired (`docs/forge/plans/live-session-assistant.md:1157-1204`). Per-trigger thresholds, e.g. T02 ≥ 0.85, T06 intent ≥ 0.8, T17 0.6, T18 and T19 0.7 (`:1346-1366`) | Per listening hour: about 180 classifier calls ($0.019 on `gpt-4o-mini`) and about 120 extraction calls ($0.045) (`docs/forge/research/live-session-assistant-cost-model.md:208-248`) | Not built | A false positive interrupts the GM (rate-limited, `:376-431`). A false negative misses help. **A missed T17 is a consent failure** | `choice` for the trigger code; `noul` for T17, T18 and T19. It cannot produce `new_names[]` or spans; it can pick `entity_ids` only from candidates |
| D12 | Workbench invocation (E-9 / RAIL-2 / CANVAS-21 / CANVAS-22) | "Tools and document edits start only from an explicit command or an explicit arming, never from the assistant's reading of a conversational message" (`docs/adr/gm-workbench-interactions.md:1040`; RAIL-2 `:133`; CANVAS-21/22 `:369-370`; NG-27 `:839`). "No model output can start work" (`docs/adr/gm-workbench-threat-model.md:236`, SEC-32) | — | — | — | **None, by rule.** See the paragraph below |

**Jev must not become a back door around E-9.** A Jev "intent" answer is model output. It may not:

- infer from chat text that the GM wants `/npc`;
- arm or keep an edit;
- turn a chip into a run;
- trigger any billable operation (X-1, `gm-workbench-interactions.md:104`).

The most it could ever do there is rank the at most three suggestion chips (RAIL-8,
`gm-workbench-interactions.md:139`), which only arm. Even that is a Workbench operation, and gate
G1 (§4) blocks it.

---

## 3. A fair comparison

### 3.1 Unit prices (list prices; estimates until the production cost records replace them)

| Item | Price | Source |
| --- | --- | --- |
| `gpt-4o-mini` | $0.15 per 1M input tokens, $0.60 per 1M output tokens | `docs/runbooks/usage-capture.md:195-196`; `additive-retrieval-web-fallback.md:302-303` |
| `text-embedding-3-small` | $0.02 per 1M tokens | `additive-retrieval-web-fallback.md:303` (checked 2026-09-16). The runbook leaves this "owner supplies" |
| OpenAI `web_search` | $10 per 1,000 calls, plus a fixed 8,000 input tokens for `gpt-4o-mini` | `additive-retrieval-web-fallback.md:298-300` |
| Jev | $0.042 per 1M input tokens; output free | [VENDOR] models.md |
| Economy candidates | $0.54 per 1,000 turns (`gpt-5-nano`, `qwen-flash-us`) against $1.26 for `gpt-4o-mini` | `game-guide-ai-model-routing.md:224-247` |

**Input size per decision [INF]:**

| Level | Input | Tokens | Calibration point |
| --- | --- | --- | --- |
| P (prompt) | Prompt, mode and the question's criteria | about 800 | liteLLM's 240 routing calls cost $0.007707; $0.007707 ÷ 240 ÷ $0.042 per 1M ≈ 765 tokens each |
| A (answer) | A 600-token answer plus criteria | about 1,000 | — |
| C (passages) | Question, top-5 chunks and criteria | about 5,500 | The routing plan assumes 6,000 input tokens per answer |

### 3.2 Cost, latency and calibration per option

**Cost per 1,000 decisions:**

| Option | P (800 tokens) | A (1,000 tokens) | C (5,500 tokens) |
| --- | ---: | ---: | ---: |
| (c) Existing heuristic or rule | $0 | $0 | $0 |
| (a) Classifier on the query embedding | $0 (the vector exists, `graph.py:124-135`) | $0.014 (embed the answer: 700 tokens × $0.02 per 1M) | $0 (distances already exist) |
| (b) `gpt-4o-mini`, one output token with logprobs | $0.12 | $0.15 | $0.83 |
| Jev | $0.034 | $0.042 | $0.23 |

**Latency, calibration and requirements:**

| Option | Added latency | Calibration | What it needs |
| --- | --- | --- | --- |
| (c) Existing heuristic or rule | About 0 | None (binary) | Nothing |
| (a) Classifier on the query embedding | Under 1 ms for P and C. For A, one embedding call (the `embedding` records give its real latency) | We own it. Platt or isotonic scaling fitted on held-out labels [INF] | Labels (hundreds per class) and a small model file. **No new vendor, no new secret** |
| (b) `gpt-4o-mini` with logprobs | One chat round trip. Unmeasured here; the `latency_ms` of `answer` and `suggestions` records bounds it | Token logprobs ([OpenAI cookbook](https://cookbook.openai.com/examples/using_logprobs), [API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions)). They must be calibrated on our labels [INF] | A prompt and calibration labels. **No new vendor:** OpenAI already sees this text |
| Jev | Vendor: 70–500 ms. Independent: p50 127 ms, p95 231 ms (liteLLM); a floor of about 430 ms through a gateway from Europe (PriorBench) | Claimed calibrated. Independently reliable only at ≥ 0.99, and only with an explicit `none` option (§1.4) | A new vendor, a new secret, a terms review, fail-open code and content-free logging |

**Where the latency lands** [INF]. `/chat` returns the whole `ChatResponse` at the end
(`service/app.py:882`), so any delay adds to the turn:

- **Prompt-level (P)** decisions can start at preflight and run alongside embedding and
  retrieval. They add only the amount by which Jev is slower than retrieval.
- **Answer-level (A)** decisions run after generation. They add their full latency to every turn
  they run on.

**Break-even.** What share of the inspected cases must be correctly skipped for a decision to pay
for itself? The formula is decision cost ÷ value of the skipped call. The value of each skipped
call is:

| Skipped call | Assumed tokens | Value per call [INF] |
| --- | --- | --- |
| Stat block | ~1,050 in / 500 out | $0.00046 |
| Suggestions + spell card | ~5,100 in / 200 out, plus ~800 in / 350 out | $0.00122 |
| Answer | 6,000 in / 600 out | $0.00126 |
| Routing downshift | $1.26 → $0.54 per 1,000 turns | $0.00072 |
| Web search | $0.010 plus 8,000 input tokens | $0.0112 |

| Decision (level) | Value of skipped call | Jev | (b) logprob | (a) embedding |
| --- | ---: | ---: | ---: | ---: |
| Stat-block veto (A) | $0.00046 | 9.1% | 33% | 3.0% |
| Spell-extras gate (A) | $0.00122 | 3.5% | 12% | 1.1% |
| Answerability → refuse (C) | $0.00126 | 18% | 66% | 0% |
| Routing downshift (P) | $0.00072 | 4.7% | 17% | 0% |
| Web-search veto (P, only on local misses) | $0.0112 | 0.3% | 1.1% | 0% |

Break-even counts dollars only. A wrong veto drops a card, a wrong downshift weakens an answer and
a wrong refusal loses one. Those costs are quality, and §5 measures them.

**Scale check [INF].** The pilot-wide cap is 500 turns a day (`config.py:235`), so at most about
15,000 turns a month. At that cap, a perfect decision could save at most:

| Decision | Monthly ceiling at the cap | Condition |
| --- | ---: | --- |
| All answers (today's spend, for scale) | about $18.90 | — |
| Stat blocks | $6.90 | Every turn fired and every call was wasted |
| Spell extras | $18.30 | Every turn was spell mode and every call was wasted |
| Routing | $10.80 | Every turn moved down a tier |
| Web fallback | about $16.80 | An illustrative 20% of turns miss locally and half of those searches are unnecessary |

Realistic savings are a fraction of these ceilings, which are well under the cost of reviewing
and running a new vendor. **At pilot scale, the case for Jev has to rest on quality or latency,
not on cost.**

### 3.3 What the production cost records will add, and when

- **Source.** Production started writing per-attempt records on 2026-09-24 (`yje.5.1.1`;
  `subscription-billing-coupons-profitability.md:216-218`). They exist only if the deploy
  actually ran, which needs `DEPLOY_TARGET` and the WIF secrets configured. There is no
  backfill, and `_Default` log retention is typically 30 days
  (`docs/runbooks/usage-capture.md:207-240`).
- **Report.** `scripts/usage_cost_report.py` prints cost by mode per day, total per account per
  month, and a count of attempts with no token counts. Every price is a required argument
  (`usage-capture.md:121-141`).
- **Derivable without code changes:**
  - turns per mode (distinct `operation_id` by `mode`);
  - the stat-block firing rate (`statblock_structuring` records ÷ sage and GM turns);
  - real tokens per `purpose`, replacing every [INF] token count above;
  - `latency_ms` per purpose (p50 and p95 by hand until `yje.5.4a`);
  - error rates per purpose.
- **Not derivable:**
  - Whether a structuring call produced a usable block. A parse failure after a provider `ok` is
    invisible, and blocks are not persisted. This needs a content-free outcome counter (bead B1,
    §6.5).
  - Any decision's quality, which needs labels.
- **When [INF]:** about 7 days of pilot traffic for the firing rates, and about 14 for the token
  distributions per mode.

---

## 4. Gates Jev must pass before it sees any user text

"Content-free shadow mode" means only that *our records* contain no text. The shadow call still
sends real text to TypeSafe, so shadow mode faces the same gates as a rollout.

| Gate | Rule | TypeSafe today | What would satisfy it |
| --- | --- | --- | --- |
| **G1** S-5 / SEC-39 / WT-20 (Workbench) | Only the primary provider may see GM-private documents. A provider joins the allowlist only under written terms that say "no training on API data and bounded retention". Adding one amends the threat model, and routing is enforced server-side (`gm-workbench-threat-model.md:238`, `:280`, `:424`) | No training: **pass**. Bounded retention: **fail** (MCA and DPA give no period; ZDR is Enterprise-only and its terms are not public) | Written ZDR or an Order Form that bounds retention, an S-5 amendment, and the WT-20 routing test. Until then, **no Workbench use at all** |
| **G2** X-7 / SEC-20 / R-11 | Private text never enters logs, traces, metric labels, error bodies or URLs (`gm-workbench-interactions.md:110`; threat model `:214`). When Langfuse tracing is on, it records prompts and completions (`:81`) | Applies to our integration, not to the vendor | Record only: decision key, confidence bucket, latency, `input_tokens`, pinned version and the realised outcome. Never the `state` or question text. Keep the Jev call out of any traced span that captures inputs |
| **G3** E-9 / RAIL-2 / CANVAS-21 / CANVAS-22 / SEC-32 | Tools and edits start only from an explicit command or one-shot arming. "No model output can start work" | — | A Jev decision may only choose how a turn the user already submitted is presented (for example, whether to structure a block). It never starts, arms or widens work (§2, D12) |
| **G4** Provider rules for `/chat`, and licensed text | A provider without API-specific retention terms is "evaluation-only with synthetic/non-sensitive data" (`game-guide-ai-model-routing.md:398-404`). "Sending licensed book excerpts to an additional provider is a separate disclosure/contract question", with a privacy-notice and subprocessor-list update (`:406-409`). Licensing review is `yje.6.1` | Same position as DeepSeek: **synthetic or public data only.** Answer-level and passage-level decisions carry licensed text; spell mode quotes rules text by design (`generate.py:199-206`) | ZDR terms, an updated privacy notice and subprocessor list, and `yje.6.1`'s ruling for any corpus excerpt |
| **G5** Owner decisions of 2026-09-21 | D-3: premium-model routing and web fallback are paid features. D-6: users are 13+, so some are minors. D-7: at 100% of the credit limit, the cheapest model is used. Credit numbers are still open (`subscription-billing-coupons-profitability.md:200-218`) | — | Run Jev decisions on those paths only for entitled users. D-7's override always beats a Jev routing decision. Size savings only after `yje.5.1.2` |
| **G6** Legal review `1ir.1.4` (P0, open) and LSA-1.5 | Four sub-reviews are open: `1ir.1.4.1` wiretap, CIPA and AI-vendor processing; `1ir.1.4.2` biometrics; `1ir.1.4.3` children and sensitive environments; `1ir.1.4.4` international scope. LSA-1.5 (`1ir.1.5`) selects vendors "with retention and DPA evidence". The LSA plan says processors "must retain nothing" (`live-session-assistant.md:1184`) | Fails "retain nothing" without ZDR | Every Live Session Assistant use waits for these reviews |
| **G7** Fail safe to today's behaviour | Early access, no SLA, limits that change without notice, 529 Overloaded, API changes that "may result in the API's becoming incompatible" (MCA) | — | Fall back to *today's* behaviour, never to a refusal, on timeout (a budget of about 300 ms [INF]), any error, 429 or 529, an open circuit, a missing key or a below-threshold confidence. No retries on the hot path. A kill-switch environment flag that needs no deploy. Pin `jev-1.13.0` |
| **G8** Cost accounting (SEC-34, `yje.5.1`) | Every billable operation is recorded (threat model `:244`; the usage runbook) | — | A new `purpose` value for Jev calls and a Jev price input in `usage_cost_report.py`, both before shadow mode |

---

## 5. Evaluation design

### 5.1 Tooling that exists or is planned

| Tool | What it does | Status | Use here |
| --- | --- | --- | --- |
| `ingestion/eval_golden.py` + `ingestion/golden_set.json` | Retrieval Precision@K and Hit@1 over **160** generated queries: rule 60, monster 33, feat 30, spell 25, dm_guidance 6, magic_item 6. Adds hand-curated cross-book queries and **5** negatives (`:123-131`), and scores koz refusals on the negatives (`:306-309`) | Exists | Labels for D2, and a small set for D4 |
| `scripts/ci/eval_gate.py` + the `retrieval-metrics` CI job (`.github/workflows/ci.yml:155-195`) | Fails CI when a headline retrieval metric regresses. Deploy depends on it (`:229`, `:256`) | Exists | Guard: a retrieval change driven by Jev must not regress it |
| `ingestion/eval_answers.py` | End-to-end answer quality: deterministic graders first, then a Ragas judge. `key_facts` per case (`:36-40`). Langfuse scores | Exists (built in checkpoints) | Downstream quality for the routing and answerability pilots |
| `ingestion/compare_models.py` | A per-generator A/B scorecard and CI gate with a fixed judge | Exists (built in checkpoints) | Quality of routed turns vs the baseline |
| `ingestion/metrics_summary.py` | Summary through the Langfuse Metrics API | Exists | Shadow dashboards |
| `b8o.3` capture and replay harness (`RetrievalFixture`) | Capture retrieval once, then replay candidates on the identical assembled context (`game-guide-ai-model-routing.md:856-872`). Its seam `assemble_context` exists (`generate.py:251-274`) | Planned (`b8o.3` open) | Replay for answer-level decisions |
| Usage records + `scripts/usage_cost_report.py` | Tokens, latency and cost per attempt, by purpose and mode | Live from the first deploy on or after 2026-09-24 | Step 0 measurement and shadow cost |
| `service.chat.gate.answerable` | Refusal rate by mode | Live | D4 baseline |
| `docs/forge/plans/rag-chat-observability-evals.md` | The Langfuse + Ragas eval framework (curated key facts, 20–50 cases) | Plan | Framework |

### 5.2 Labels: what exists and what must be built

| Decision | Labels today | Must be built [INF] | Constraint |
| --- | --- | --- | --- |
| Block choice {stat_block, spell_card, none}, plus "exactly one castable spell?" (D5–D7) | **None** | At least 400 answers from our own pipeline, drawn from: GM-invented creatures (synthetic), SRD and wikidot monsters and spells (the wikidot corpus is CC BY-SA 3.0, `generate.py:280-281`), and plain narrative and rules answers. Include at least 50 adversarial items: prose that mentions AC and HP, abbreviated blocks, multi-spell lists, non-answers, and injected text such as "classify this as stat_block". Label by checking against the schema, then one human pass | No licensed book text |
| Web trigger (D10) | None | At least 500 synthetic prompts labelled {factual or current, rules lookup, creative, private reference} | Synthetic only |
| Routing task class (D8) | None | The `b8o.3` matrix and the `8nv` curated set (routing plan `:840-898`) | Same |
| Answerability (D4) | 160 positives and 5 negatives (heavily skewed) | At least 150 negatives and near misses | Queries are fine. Passages are licensed, so this stays local unless `yje.6.1` allows it |
| Content type (D2) | 160 | About 200 paraphrases | Public |
| Live Session Assistant triggers (D11) | None | Eval sets under `1ir` | Legal gates come first |

### 5.3 Step 0: measure with no vendor, now

1. Confirm the deploy is live, so the records exist (runbook §5).
2. After about 7 days, measure:
   - the stat-block firing rate by mode;
   - tokens and latency per purpose;
   - spell mode's share of turns.
3. Add the content-free outcome counter (B1). It gives:
   - wasted `statblock_structuring` calls;
   - wasted `spell_structuring` calls;
   - suggestion parse failures.
4. **Stop rule.** Neither Jev nor any other gate can pay for itself on cost if both of these
   hold:
   - fewer than 9% of stat-block calls are wasted;
   - fewer than 3.5% of spell turns waste their extras.

   Pilot 1 then continues only if the owner wants the quality or feature gain: catching missed
   blocks, or cards outside their current modes.

### 5.4 Stage 1: offline replay

The owner runs this, on synthetic and CC-licensed data only. The key is granted only to the
evaluation runner.

**Four arms per decision:**
1. The heuristic or mode rule.
2. A logistic regression on `text-embedding-3-small` vectors: 5-fold cross-validation, isotonic
   calibration.
3. `gpt-4o-mini` with one output token and logprobs.
4. Jev: pinned `jev-1.13.0`, an explicit `none` option, a fixed option order and reviewed
   criteria.

| Metric | Definition |
| --- | --- |
| Agreement | Accuracy, macro-F1 and per-class precision and recall. Split the confusions that cost money from the ones that cost quality |
| Calibration | Reliability curve, ECE (10 bins), and coverage and precision at thresholds of 0.90, 0.95 and 0.99. The share of 50 out-of-scope items flagged |
| Latency | p50 and p95, measured from the GCP region Cloud Run uses |
| Cost | Per 1,000 decisions, from each provider's reported `usage` |
| Downstream | Structuring calls or searches avoided or added × $ per call; cards lost or gained; for routing, the `b8o.3` quality difference |
| Robustness | The largest swing when the option order is permuted; a paraphrase set; the injection items |

### 5.5 Stage 2: content-free shadow in production

This stage comes only after gates G1 and G4 are cleared for the text class involved, and after
Stage 1 passes.

- **How it runs.** Call Jev in parallel and never act on its answer.
- **Sample.** 5–10% of eligible turns, keyed by `operation_id`.
- **What is recorded.** Only:
  - the decision key;
  - a confidence bucket (<0.9, 0.9–0.99, ≥0.99);
  - `latency_ms` and `input_tokens`;
  - the pinned version;
  - the baseline's decision;
  - the realised outcome.

  No text is recorded (X-7).
- **Timeout.** 300 ms. Failures are counted.
- **Duration.** 2–4 weeks, or at least 2,000 decisions.
- **Check.** Compare the shadow numbers with the offline ones and watch for drift.

### 5.6 Stage 3: flagged rollout

- **Flag.** One environment flag per decision, off by default.
- **When Jev acts.** Only in the direction Stage 1 approved, and only at or above the chosen
  threshold. For example, skip structuring only when Jev returns `none` at ≥ 0.99. Otherwise the
  turn behaves as today.
- **Canary steps.** Owner only, then 10%, then 50%, then 100%.
- **Stop rules.** Stop and roll back on any of:
  - card loss above the heuristic's;
  - p95 turn latency up by more than 100 ms;
  - Jev errors and timeouts above 1%.
- **Rollback.** Turn the flag off. No deploy is needed.

---

## 6. Recommendation

### 6.1 Ranking by expected value [INF]

| Rank | Decision | Value of one decision | Quality upside | Blockers | Expected value now |
| --- | --- | --- | --- | --- | --- |
| 1 | Web-fallback trigger (D10) | Highest: about $0.0112 per avoided search | High | `xiu.1.4` is unbuilt and blocked by `yje.1.1`; G4 | High later; zero now |
| 2 | Block choice and spell-extras gate (D5–D7) | Low: $0.00046–$0.00122 | Medium: catching missed blocks. Showing cards outside their modes would be a feature change | G4 blocks real answers. An offline pilot on synthetic and CC data is possible now | Low to medium. **The only candidate that can be tested now** |
| 3 | Answerability (D4) | $0.00126 | High: the additive-retrieval plan exists to relax the strict gate (`additive-retrieval-web-fallback.md:30-56`) | G4 (licensed passages) and `yje.6.1` | Medium, blocked |
| 4 | Routing (D8) | At most $0.00072 | Unknown: nobody has measured quality downstream of routing (§1.4) | `b8o.3`; the plan's "no router-model call" rule; only one model enabled | Low now |
| 5 | Live Session Assistant triggers (D11) | About $0.015 per listening hour on the classifier ($0.019 → about $0.0043) | Medium; T17 needs low latency | Legal P0 reviews, "retain nothing", Phase 3 | Later |
| 6 | Content type and rerank gate (D2, D3) | About $0 (no paid call; rerank is inert in production) | Low | None | Useful only as a calibration probe |
| — | D1, D9, D12 | — | — | — | **Not candidates.** D12 is forbidden |

### 6.2 Pilots and pass thresholds

**Pilot 1: block choice, offline.** It runs after Step 0 and uses the §5.2 set.

- **Question:** a `choice` over {stat_block, spell_card, none} on the generated answer, plus a
  `noul` asking "is this about exactly one castable spell?".
- **Pass requires all of the following:**
  1. Jev's macro-F1 is at least **5 points** above the best near-free arm.
  2. At the action threshold (expected ≥ 0.99), the `none` veto has precision **≥ 0.98** and
     covers **≥ 50%** of the true `none` cases among heuristic positives.
  3. **≥ 90%** of the 50 adversarial and out-of-scope items land on `none` or below the
     threshold. Permuting the option order moves results by **≤ 3 points**.
  4. p95 is **≤ 300 ms** from the service's region, and errors plus timeouts are **≤ 1%**.
  5. At Step 0's measured rates, the avoided calls × $ per call exceed the decisions × $0.000042,
     and Jev **loses no more cards than the heuristic does today**.
- **If Jev fails the first test** but the embedding arm passes tests 2 to 5, adopt the embedding
  arm. It needs no new vendor.

**Pilot 2: web-fallback eligibility, offline.** It starts once `xiu.1.4` defines the eligibility
step.

- **Question:** a `choice` over {factual or current, rules lookup, creative, none} on the prompt
  and mode.
- **Data:** at least 500 synthetic prompts.
- **Pass requires all of the following:**
  1. Jev cuts **unnecessary searches by ≥ 25%** relative to the best near-free arm, at an equal or
     lower missed-search rate.
  2. p95 is **≤ 300 ms**, so it finishes alongside retrieval.
  3. Cost is **≤ $0.05 per 1,000 decisions**.
  4. The 50 out-of-scope and injection items trigger no search.

### 6.3 What the owner must do, in order

1. **Decide whether any new AI processor is acceptable,** even for synthetic-only offline work.
   The first API call accepts the MCA.
2. **Review the TypeSafe documents:**
   - the MCA (2026-09-23), DPA (2026-04-24), Privacy Policy (2025-11-19) and AUP (2026-09-23);
   - the retention wording;
   - the perpetual Telemetry clause ("classifications … and learnings").

   **Ask TypeSafe for:**
   - the subprocessor list;
   - ZDR terms and price;
   - a written retention bound for inputs, outputs and logs;
   - a narrower Telemetry clause;
   - SOC 2 status.
3. **Request early access** at `console.typesafe.ai`. The owner does this, not an agent.
4. **Put the key in Google Secret Manager** (for example `typesafe-api-key`). Grant it only to
   the offline evaluation runner's service account. The Cloud Run service does not get it before
   Stage 2.
5. **Confirm the deploy is live** (`DEPLOY_TARGET` and WIF) so the `yje.5.1.1` records flow, and
   supply the embedding price (runbook §4).
6. **Only if a pilot passes:**
   - update the privacy notice and subprocessor list;
   - rule on licensed excerpts under `yje.6.1`;
   - decide whether to reopen the routing plan's "no router-model call" rule.

   Workbench use would further need an S-5 amendment. None of this is needed for `/chat`
   offline work.

### 6.4 What would make me recommend NOT adopting Jev

- **Terms.** No written bounded retention or ZDR. Jev would then stay, at most, an offline tool
  used on synthetic data.
- **Telemetry.** The Telemetry clause stays unchanged, and the owner rejects perpetual use of
  derived "classifications … and learnings".
- **Stage 1.** Jev fails to beat the best near-free arm by the stated margins, or either of
  these holds:
  - at ≥ 0.99 confidence it covers less than half the cases;
  - even with an explicit `none` option, out-of-scope inputs are still not flagged.
- **Operations.** Any of these:
  - p95 above 300 ms from our region;
  - errors and timeouts above 1%;
  - rate limits below our peak;
  - version changes that move tuned thresholds by more than 3 points.
- **Step 0.** The measured waste is below break-even (stat block under 9%, spell extras under
  3.5%), and no quality case exists.
- **Volume.** Through the next pricing review, volume stays near the pilot cap. There, every
  candidate's ceiling is at most about $20 a month (§3.2), less than reviewing and operating a
  new vendor.

### 6.5 Proposed Beads tasks (not created; for the lead)

| ID | Title | One line | Priority | Blocked by |
| --- | --- | --- | --- | --- |
| B1 | Record content-free structuring outcomes per mode | A bounded counter: block produced / `None` / parse failure / skipped by gate, for stat blocks, spell cards and suggestions. No text. It makes wasted calls measurable, and the credit numbers need it too | P2 | — |
| B2 | Step-0 report: stat-block and spell-extras waste, tokens and latency per purpose | Run `usage_cost_report.py` and B1 over about 7 days of `yje.5.1.1` records. Apply the §5.3 stop rule | P2 | B1; deploy confirmed |
| B3 | Build the block-choice labelled set (≥ 400) from synthetic and SRD/wikidot content | Includes 50 adversarial and injection items. No licensed book text | P3 | — |
| B4 | Offline decision-benchmark harness (four arms) | Heuristic vs embedding logistic regression vs `gpt-4o-mini` logprob vs Jev. Reports F1, ECE, coverage at thresholds, p50/p95, cost per 1,000 and downstream $. The Jev arm runs only with a key the owner provisions | P3 | — |
| B5 | Owner: review TypeSafe's terms and decide on early access | MCA, DPA, ZDR, Telemetry, subprocessors, SOC 2. If yes: request access and put the key in Secret Manager for the evaluation runner only | P2 | — |
| B6 | Pilot 1: run the block-choice benchmark and report against §6.2 | Decide between Jev, the embedding arm and the status quo | P3 | B2, B3, B4, B5 |
| B7 | Pilot 2: web-fallback eligibility benchmark | The §6.2 thresholds. Synthetic prompts only | P4 | `xiu.1.4`, B4, B5 |
| B8 | Production integration of a passed pilot | Adds: a `decision` usage purpose and Jev price; a fail-open client with timeout, circuit and kill switch; content-free shadow logging; a pinned version | P4 | A passed B6 or B7; G1/G4 cleared |

### 6.6 Open questions only the owner can answer

1. Will you accept a new AI processor for `/chat` text during the private pilot? If so, only
   with ZDR? Would you pay for TypeSafe Enterprise to get it?
2. Is the MCA's perpetual Telemetry clause ("classifications … and learnings") acceptable?
3. Do you still want the routing plan's rule of no router-model call? A Jev router contradicts
   it.
4. Is "block choice" a feature you want, meaning spell cards or stat blocks outside their current
   modes? Or is it only a cost gate?
5. What turn volume do you expect after launch? At the pilot cap, every candidate's ceiling is at
   most about $20 a month.
6. May licensed book excerpts (answers that quote rules text, and retrieved passages) go to an
   additional provider (`yje.6.1`)?
7. For the Live Session Assistant, would a decision-model vendor be considered under LSA-1.5 at
   all?

---

## Sources

All pages were read on 2026-09-24.

**Vendor (TypeSafe AI).** Claims only.

- [typesafe.ai](https://typesafe.ai/)
- [Launch blog](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [Docs index](https://docs.typesafe.ai/llms.txt)
- [API reference](https://docs.typesafe.ai/api.md)
- [Models](https://docs.typesafe.ai/models.md)
- [Choice](https://docs.typesafe.ai/primitives/choice.md)
- [Advanced (structure)](https://docs.typesafe.ai/primitives/advanced.md)
- [Confidence](https://docs.typesafe.ai/confidence.md)
- [jev-1.13 known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)
- [Extraction cascade cookbook](https://docs.typesafe.ai/cookbooks/sde_cascade.md)
- [Quickstart](https://docs.typesafe.ai/introduction/quickstart.md)
- [Python SDK constants](https://docs.typesafe.ai/sdk/python/api/constants.md)
- [Python SDK changelog](https://docs.typesafe.ai/sdk/python/changelog.md)
- [Legal index](https://docs.typesafe.ai/legal.md)
- [Master Customer Agreement](https://typesafe.ai/legal/mca)
- [Data Processing Agreement](https://typesafe.ai/legal/data-processing)
- [Privacy Policy](https://typesafe.ai/legal/privacy-policy)
- [Terms of Use (site)](https://typesafe.ai/legal/terms)
- [Acceptable Use Policy](https://typesafe.ai/legal/acceptable-use-policy)
- [Trust center](https://trust.typesafe.ai/) and [subprocessors](https://trust.typesafe.ai/subprocessors) (both showed no content)
- [Workflow evals](https://evals.typesafe.ai/)

**Independent**

- [liteLLM: Jev auto-router benchmark](https://docs.litellm.ai/blog/jev-auto-router-benchmark)
- [PriorBench: pre-registered evaluation of Jev](https://github.com/priorbench/jev)
- [Tom's Hardware](https://www.tomshardware.com/tech-industry/artificial-intelligence/typesafe-ais-jev-offers-an-alternative-to-llms-that-claims-to-be-193x-faster-and-445x-cheaper-system-one-type-model-is-bespoke-for-probabilistic-decision-making) (headline only; press restating vendor claims)

**OpenAI (baseline b)**

- [Using logprobs (cookbook)](https://cookbook.openai.com/examples/using_logprobs)
- [Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions)

**Repository.** Every `path:line` citation above, at commit `2770766`. One file lives outside
the repository: the owner-decision record at
`agent-forge-harness/.tmp/work/owner-decisions-2026-09-21.md`. Its in-repo copy is the billing
plan's section "Owner decisions 2026-09-21".
