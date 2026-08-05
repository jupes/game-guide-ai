# Research and implementation plan: conversation memory and response personalization

Generated: 2026-07-25
Repo: `game-guide-ai`
Companion to: `docs/forge/plans/game-guide-ai-model-routing.md` (the routing plan). That plan's
decisions D1–D7 are inherited here and not reopened.
Status: draft for review; no plan-review pass has run against this document yet.

## Executive decision

Give the application three tiers of memory — short (recent turns), medium (a rolling conversation
summary), and long (a per-user profile of facts and response-style preferences) — plus a
personalization system that starts with explicit user controls and only later learns implicitly
from feedback signals.

One rule makes the whole design compatible with model routing, and it is the answer to "what
happens to context when the model changes":

> **M-RULE: All memory is model-agnostic plain text, assembled into the prompt fresh on every
> turn. No tier may ever depend on provider-side state — reasoning blocks, cache handles,
> server-side conversation objects, or preserved thinking.**

Because every turn is assembled from neutral text, *any* model can serve *any* turn. `Auto` may
pick a different model on every request and nothing is lost or corrupted; a manual model change
(which starts a new conversation under the routing plan) can carry the old conversation's summary
forward so the user does not lose context. Provider-side caching or preserved reasoning may be
exploited later as a per-provider *optimization*, never as a correctness dependency.

Delivery is staged M1→M4, each independently shippable and gated on a faithfulness evaluation:

1. **M1 — short-term:** replay a token-budgeted window of recent turns into generation.
2. **M2 — medium-term:** maintain a rolling conversation summary; carry it forward when a model
   change starts a new conversation.
3. **M3 — explicit personalization:** bounded, user-set response-style preferences applied to
   every turn. No account required.
4. **M4 — long-term (post-auth):** a server-side per-user profile combining durable facts and a
   learned style memo derived from feedback signals. Blocked on invite-based authentication.

## How this interacts with model switching

The routing plan pins the *requested strategy* per conversation: `manual:<alias>` stays fixed,
`auto` reclassifies every turn, and changing strategy or alias starts a new conversation. Two
switching scenarios exist, and memory must be safe under both:

| Scenario | Frequency | What memory must guarantee |
|---|---|---|
| `Auto` picks a different model on the next turn | Potentially every turn | The next model sees exactly the same assembled memory text the previous model would have. Nothing provider-specific persists between turns. |
| User changes model/strategy → new conversation | Occasional, user-driven | The new conversation may be seeded with the previous conversation's summary (M2 carry-forward), so "switch model" does not mean "lose everything". |

Both guarantees fall out of M-RULE. This is also why the routing plan's warning about Moonshot's
preserved-thinking continuity stands: replaying provider reasoning content would bind a
conversation to one provider and silently break `Auto`. It stays out of scope.

**Cost interaction.** The routing plan's cost model assumes ~6,000 input tokens per turn. Memory
adds input tokens on top: the M1 history window (budget ~1,500–2,500 tokens), the M2 summary
(~300–500 tokens), and M3 preference text (~50–150 tokens). That is roughly a 15–50% input-cost
increase at list price, and it also shifts the routing classifier's inputs (a short follow-up
question with a large history window is still a "lookup"). Re-run the routing plan's cost
sensitivity with measured window sizes before setting tier ceilings, and make the window budget a
config value, not a constant.

**Prompt-cache interaction.** Providers cache by prefix. Order the assembled prompt
stable-to-volatile so caches can actually hit: persona + style preferences (rarely change), then
summary (changes occasionally), then retrieved sources and history window (change every turn),
then the question. Do not project cache-hit savings until measured — the routing plan's warning
about Kimi's coding-workload cache rates applies with more force once a sliding window churns the
prompt tail.

## Current state (what exists to build on)

- `chat.messages` already stores every turn with `conversation_id`, `mode`, `role`, `content`,
  `suggestions`, `created_at` (`service/history.py`, `CHAT_SCHEMA_DDL`), and
  `MessageStore.recent(conversation_id, limit)` already reads the most recent N. Short-term memory
  is a read path away — the data is there.
- Generation is strictly single-turn: `generate_answer` sends one system message and one user
  message built from `GROUNDED_TEMPLATE = "Sources:\n{context}\n\nQuestion: {question}\n\nAnswer:"`
  (`service/generate.py:97`). Nothing reads `chat.messages` into a provider request.
- The routing plan's D2 extracts `assemble_context()` as a pure function. Memory extends that seam:
  prompt assembly stays pure, testable, and shared with the eval harness.
- The routing plan's conversation-strategy record (`chat.conversations`) gives M2 a natural home
  for summary columns.
- There is no authentication. Conversation IDs are browser-generated UUIDs in localStorage. This
  hard-bounds what M1–M3 may store server-side and is why M4 waits for auth.

## Tier design

### Short-term: the history window (M1)

Replay the last N turns of the current conversation, newest-first truncation under a token budget,
as plain alternating user/assistant messages between the system prompt and the grounded question.

- Read through `MessageStore.recent()`; never a second store.
- Budgeted by estimated tokens, not message count; drop oldest first; never truncate mid-message.
- Attachment text is not duplicated into history (it is already injected as a numbered source).
- History is **continuity, not evidence**. The grounding contract must survive: facts still come
  only from the numbered sources. The persona/template gains an explicit instruction to that
  effect ("use the conversation for context and follow-ups; answer facts only from the sources").
  The single biggest quality risk in this plan is history diluting grounded faithfulness — the M1
  eval gate below exists for exactly that.
- The refusal path gains nuance: a follow-up like "and at 5th level?" is only answerable *with*
  history. Retrieval still runs on the raw prompt in M1; query rewriting from history (turning
  "and at 5th level?" into "fireball damage at 5th level") is a deliberate follow-up experiment,
  not part of M1, because it changes retrieval behavior and needs its own eval.

### Medium-term: the rolling conversation summary (M2)

When a conversation's history exceeds the M1 window, maintain a compact summary of what scrolled
out of it: settled facts, user goals, named entities (the user's PC, the homebrew campaign), open
threads. Store it on the conversation record; regenerate asynchronously (or post-response) every K
turns with the economy-tier model; inject it as a clearly delimited block above the history window.

- Columns on `chat.conversations`: `summary_text`, `summary_through_message_id`,
  `summary_updated_at`, `summary_model_alias`. Bounded length (~500 tokens); regeneration replaces,
  never appends.
- Summary generation is a `call_purpose` of its own (`summary`), routed to the economy tier,
  budget-capped, and observable exactly like answer/suggestion attempts under the routing plan's
  attempt-observer contract. A failed summary refresh degrades silently — the conversation
  continues on the stale summary.
- **Carry-forward:** when the user changes model/strategy and accepts the "start a new
  conversation" flow, the UI may offer "bring context along", which copies the old conversation's
  summary (plus a summary of the still-in-window turns) into the new conversation's record. This
  is the design answer to "what happens to context when I switch models": it survives as text.
- The summary is user content. It is sent to whichever provider serves the turn, so it inherits
  the routing plan's data-policy gates (see Privacy below), and it must be deleted when the
  conversation's messages are deleted (routing plan D6 retention applies to summary columns too).

### Long-term: the user profile (M4, post-auth)

Cross-conversation memory keyed to an authenticated user, two distinct parts:

- **Durable facts** the user states or confirms: "my character is a level 6 tiefling warlock",
  "we use the optional flanking rule". Explicitly saved ("remember this"), listed in the profile
  UI, individually deletable. Never inferred silently.
- **Style memo** — see Personalization below.

Both are injected as delimited text blocks in the stable prompt prefix. Both are bounded in size.
Both are fully user-visible and user-editable: memory the user cannot inspect is a trust and
privacy defect, not a feature. M4 is blocked on invite-based authentication (already a named
dependency in the routing plan) and on encrypted per-user storage; nothing in M1–M3 may create a
de-facto user identifier to work around that.

## Personalization: learning how a user likes their responses

Explicit before implicit — a settings panel beats an inference engine on transparency, cost,
determinism, and privacy, and it ships without auth.

### M3 — explicit preferences (no account needed)

A small set of **bounded enum** preferences, set in the UI, stored in localStorage beside the
existing theme/conversation state, sent with every `/chat` request, validated server-side:

| Preference | Values (v1) | Prompt effect |
|---|---|---|
| `verbosity` | `concise` / `standard` / `detailed` | Response length guidance in the persona block |
| `rules_citations` | `inline` / `end` / `minimal` | How sources are referenced in the answer text |
| `stat_block_format` | `full` / `compact` | GM-mode stat block rendering |
| `tone` | `neutral` / `in_character` | How strongly the persona voice colors answers |

Bounded enums, not free text: a free-text "custom instructions" field is a prompt-injection
surface and an eval nightmare, and it is deliberately **excluded from v1**. The server maps each
enum to reviewed prompt fragments; the client never sends prose that reaches the system prompt.
Preferences ride the request (like `mode` does today), so the server stores nothing and the
feature needs no account. Each fragment set gets eval coverage before shipping — "concise" must
shorten answers without dropping citations or correctness.

### M4 — implicit style learning (post-auth)

Prerequisite: feedback affordances that do not exist yet (thumbs up/down and a "regenerate"
action on answers — a small UI feature that M4 needs landed first; regenerate outcomes are also a
quality signal the eval work can use independently).

The mechanism: periodically (not per-turn), an economy-model job distills a user's accumulated
signals — ratings, regenerations, explicit preference changes, message lengths of well-rated
answers — into a short **style memo** ("prefers short answers with page citations; likes tables
for spell comparisons; dislikes flowery narration outside GM mode"). The memo is:

- bounded (~150 tokens), regenerated wholesale, never appended;
- shown verbatim in the profile UI, editable and deletable by the user;
- injected as one delimited block in the stable prefix;
- A/B gated: memo-on must beat memo-off on user ratings without regressing grounded faithfulness,
  or it does not ship on by default.

No per-turn learning, no fine-tuning, no cross-user pooling. If the memo cannot demonstrate lift
in the A/B gate, keep explicit preferences and stop — M3 alone already delivers most of the value.

## Privacy, policy, and safety

- **Memory expands what a provider sees.** Today one turn sends one prompt; with M1/M2 a turn
  sends the recent transcript and a distilled summary of the whole conversation. Under the routing
  plan's policy attributes this is user content of the same class as the prompt itself — but the
  *aggregation* is more sensitive than any single message. Consequence: a provider approved only
  for synthetic/non-sensitive data (DeepSeek direct, per the routing plan) must not receive
  memory-bearing traffic; the eligibility check runs against the assembled request, not just the
  current prompt.
- **Fallback across providers** now moves accumulated context, not one message. The routing
  plan's rule already covers this (a fallback may not cross the content-policy boundary); this
  plan just raises the stakes of enforcing it.
- Summaries, profiles, and style memos are derived personal data: user-visible, user-deletable,
  covered by the D6 retention job, never placed in metric labels or Langfuse metadata beyond
  bounded flags (`memory_present: true/false` is fine; memory text is not).
- History injection is a prompt-injection surface (a past turn can contain adversarial text that
  is now replayed forever). Delimit history and summary blocks clearly, keep the grounding
  instruction dominant, and include injection-shaped cases in the M1 eval set.

## Evaluation gates

Extend the routing plan's evaluation matrix; both efforts share the capture/replay harness and the
D2 `assemble_context()` seam.

| Stage | New eval cases | Gate |
|---|---|---|
| M1 | Multi-turn follow-ups ("and at 5th level?", pronoun references); injection-shaped history; long-window grounding checks | No regression in grounded faithfulness or refusal precision vs. the stateless baseline; follow-up answer quality materially improves |
| M2 | Summary factual-consistency (summary claims vs. transcript); carry-forward continuity across a model switch | Zero fabricated facts in summaries on the eval set; carried-forward conversations answer follow-ups correctly |
| M3 | Each preference fragment × each mode | Preference honored (measured: length, citation form, structure) without correctness/citation loss |
| M4 | Memo-on vs. memo-off A/B | Rating lift without faithfulness regression |

M1's gate is the one that can kill a checkpoint: if a replayed window measurably degrades
groundedness and prompt-engineering cannot recover it, ship a smaller window or hold M1 behind the
flag — do not trade faithfulness for continuity.

## Build sequence

Each stage is a separate PR tied to its Bead, same conventions as the routing plan.

1. **M1** — history window. Extend prompt assembly (on the D2 seam) with a budgeted window read
   through `MessageStore.recent()`; template/persona continuity instruction; `RAG_HISTORY_WINDOW`
   config (0 disables = current behavior, the default until the eval gate passes); multi-turn eval
   cases. Depends on routing `b8o.1` (assembly seam). Nothing else changes: same model, same
   retrieval.
2. **M2** — rolling summary + carry-forward. Summary columns, async refresh with
   `call_purpose="summary"` on the economy route, delimited injection, carry-forward flow in the
   new-conversation dialog. Depends on M1 and on routing `b8o.2` (the conversation record and
   new-conversation flow exist there).
3. **M3** — explicit preferences. UI settings panel + localStorage; bounded enums on
   `ChatRequest`; server-side fragment mapping; per-preference eval. Independent of M1/M2; can
   ship first if sequencing demands.
4. **M4** — feedback affordances, then the style memo job and profile UI. Blocked on invite-based
   authentication; do not start before it.

Out of scope for this plan: vector/RAG retrieval over past conversations, cross-user or
campaign-shared memory, provider preserved-reasoning replay, fine-tuning, free-text custom
instructions, and any client-side scraping of behavior beyond the named feedback signals.

## Open decisions that require evidence

1. Window size and summary refresh cadence (K) — set from measured token counts and the M1/M2
   evals, not guessed.
2. Whether query rewriting from history (for retrieval of follow-ups) earns its complexity — M1
   ships without it; measure how often retrieval misses on follow-up phrasing first.
3. Whether the M2 summary should be generated per-mode (a GM-channel summary reads differently
   from a rules-channel one) or uniformly.
4. Which feedback signals actually predict preference (M4 A/B decides whether the memo ships at
   all).
5. Retention window for summaries/profiles — decided together with the routing plan's D6 window.
