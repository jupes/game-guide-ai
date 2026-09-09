# Ship report: chat reading experience

Shipped: 2026-09-03
Repo: `game-guide-ai` · Branch `feat/pp6q.1-chat-reading-experience`
Beads: `agent-forge-harness-pp6q.1` (CP1–CP5 all closed) under epic `agent-forge-harness-pp6q`

Phase 1 of the chat-experience overhaul. UI-only; **no backend change**. The `Exchange`-pair
model is untouched — Phase 2 (`pp6q.2`) replaces it.

## Before / after

| | Before | After |
|---|---|---|
| Answer body | One raw string — `## Fireball` and `**bold**` arrived as literal syntax | Headings, lists, GFM tables, inline + fenced code, blockquotes, links |
| Scroll | None at all. New answers landed below the fold; the reader had to find them | Follows the newest message while at the bottom; holds position when scrolled away, offering jump-to-latest |
| Feed width | Unconstrained — prose ran the full width of the viewport | Centred 48rem reading column on the parchment ground the DS ships |
| Composer | Fixed `rows={2}`; a long prompt scrolled inside a two-line box | Grows to fit, caps at 200px, then scrolls internally |
| Thinking state | The literal text "Consulting the tomes…" | Three-dot animation, with the announcement preserved for assistive tech |

## Commits

| SHA | Checkpoint |
|-----|-----------|
| `0d8b66f` | CP1 — sanitized markdown rendering |
| `e5d5dc8` | CP2 — reading column + parchment ground |
| `1650d76` | CP3 — autoscroll + jump-to-latest |
| `6ceed24` | CP4 — `ds/TextField` `autoGrow` + composer |
| `39bafdb` | CP5 — typing indicator |

## Verify it yourself

```bash
cd repos/game-guide-ai/ui && bun install && bun run dev
```

Then, with the stack running (or with `/chat` stubbed, as the checkpoint demos did):

1. **Markdown** — ask a spell question. The answer arrives with real headings, a table and lists
   instead of one undifferentiated block.
2. **Reading column** — widen the window. Prose stays in a centred column; the parchment ground
   and scrollbar stay full-bleed.
3. **Autoscroll** — send several messages, then scroll up mid-answer. The feed stops following and
   offers *Jump to latest*; a new answer arriving while you read does **not** move you.
4. **Composer** — type a long multi-line prompt. The field grows, caps, then scrolls. Enter still
   sends; Shift+Enter still inserts a newline.
5. **Typing dots** — send anything and watch the pending bubble.

```bash
bun run test        # 664 pass
bun run typecheck && bun run lint && bun run build
```

## Test evidence

664 UI tests (up from 648 at branch point), typecheck / lint / build clean.

Every checkpoint was also **driven in a real browser** via Playwright with the API stubbed, not
just asserted in jsdom. That caught things unit tests structurally cannot:

- **CP3's 32px threshold is empirical.** Measured live, the residual distance after auto-scrolling
  to the bottom is **~2px**, not 0. An exact `scrollTop === scrollHeight - clientHeight` check
  would have classified a reader who never moved as "scrolled away" and silently stopped
  following. jsdom, which reports 0 for every scroll property, could never have shown this.
- **CP4's shrink-back path.** Live: 24px empty → 96px at four lines → capped 200px with
  `overflow-y: auto` at forty → back to 24px when cleared. That last transition is the one the
  reset-before-measure logic exists for, and it is invisible without layout.
- **CP5 in both motion modes.** `animation-name: chat-pane-bounce` normally, `none` under
  `prefers-reduced-motion`, with the status text present and visually hidden in both.

## Decisions worth carrying forward

- **D1 — `marked` + `dompurify` (3 packages) over `react-markdown` + `remark-gfm` (95).**
  Measured by installing both. `ui/` has three runtime dependencies by design and the repo carries
  an advisory backlog. The trade is that sanitization is now ours, so the nine XSS vectors are
  pinned as tests — including one asserting a **normal link survives**, because a sanitizer that
  strips everything passes all the negative tests while being completely broken.
- **D2 — no syntax highlighting.** Styled `pre`/`code` on the existing mono token.
- **D3 — `autoGrow` on `ds/TextField`, defaulting off.** A documented DS extension in the same
  category as the `onKeyDown` extension already in that file. Proposed upstream as
  `agent-forge-harness-xmzf.7` so the deviation converges rather than becoming permanent drift.

## Things found along the way

- **Two CP3 tests were initially vacuous** — they asserted the empty-thread guard rather than the
  at-bottom check they were named for, because they never sent a message. Fixed to populate the
  thread first. Worth noting as a pattern: a negative assertion (`queryBy… toBeNull`) passes
  trivially before the feature exists, so it proves nothing on its own.
- **A research probe produced a false XSS alarm.** A regex over serialized HTML flagged
  `javascript:` as leaking, but `marked` had swallowed the link into a raw-HTML block so it never
  became a link. All assertions now run against parsed DOM, where inert text cannot be mistaken
  for a live attribute.
- **The `.chat-pane__empty` centring nearly regressed.** Its `margin: auto` resolved against the
  scroller, which CP2 turned into a non-flex container. Caught in the live check (measured 0px off
  centre after adding `flex: 1` to the new column).

## Follow-ups filed

- `agent-forge-harness-xmzf.7` (P3) — propose `autoGrow` (and the pre-existing `onKeyDown`
  extension) upstream to `aetheril-design-system`.

## Recommended, not filed

Every checkpoint here needed a hand-written Playwright driver (stub `/auth/me`, `/models`,
`/conversations/**`, `/chat`; click through *Enter the Tavern* → *New conversation*). That is the
second feature in a row to rebuild the same scaffolding. A project run-skill capturing it would
pay for itself — `/run-skill-generator` in `repos/game-guide-ai`.
