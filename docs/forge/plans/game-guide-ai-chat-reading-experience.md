# Plan: chat reading experience (Phase 1 of the chat-experience overhaul)

Generated: 2026-09-03
Repo: `game-guide-ai`
Beads: `agent-forge-harness-pp6q.1` (epic `agent-forge-harness-pp6q`)
Research: `docs/forge/research/game-guide-ai-chat-reading-experience.md`

Five checkpoints, each independently demo-able with `bun run dev`. No architecture change — the
`Exchange`-pair model stays exactly as it is; Phase 2 (`pp6q.2`) replaces it.

## Build sequence

| CP | Bead | What | Why here |
|----|------|------|----------|
| 1 | `pp6q.1.1` | Sanitized markdown rendering | Biggest single win; everything else is framing for it |
| 2 | `pp6q.1.2` | Reading column + parchment ground | Column width is a judgment about *rendered prose* — set it against real markdown, not empty bubbles. **Depends on CP1.** |
| 3 | `pp6q.1.3` | Autoscroll + jump-to-latest | Independent of 1–2 |
| 4 | `pp6q.1.4` | Auto-grow composer (`ds/TextField`) | Independent; touches the shared `ds/` layer |
| 5 | `pp6q.1.5` | Typing indicator dots | Smallest; pure polish |

Only CP2→CP1 is a hard dependency. The rest could reorder, but this order maximises
demo value per step.

## TDD strategy

Red → green per behavior, one test at a time, per `.claude/skills/tdd/SKILL.md`.

### CP1 — markdown

Tracer bullet: `**bold**` renders as `<strong>` through the real `marked`+`DOMPurify` pipeline.

Rendering behaviors:
1. `**bold**` → `<strong>`
2. `## heading` → `<h2>`
3. `- a\n- b` → `<ul><li>`
4. GFM table → `<table>` (no plugin — `marked` ships GFM)
5. Fenced block → `<pre><code>`
6. Inline `` `code` `` → `<code>`
7. Inline `[1]` citation markers survive as visible text

Sanitization behaviors — **the research-verified vector list, one test each**:

| # | Input | Expected |
|---|-------|----------|
| 8 | `<script>alert('xss')</script>` | no `<script>` in output |
| 9 | `<img src=x onerror=alert(1)>` | `<img>` may remain; no `onerror` attribute |
| 10 | `[c](javascript:alert(1))` | anchor without an href |
| 11 | `[c](JaVaScRiPt:alert(1))` | anchor without an href (case-insensitivity is the point) |
| 12 | `[c](data:text/html;base64,…)` | anchor without an href |
| 13 | `<a href="javascript:…">` raw | anchor without an href |
| 14 | `<svg onload=…>` | no `onload` attribute |
| 15 | `<iframe src=…>` | removed |
| 16 | `[docs](https://example.com)` | **href preserved** |

Row 16 is not optional. A sanitizer suite that only proves things get stripped can pass while
the sanitizer strips *everything* — the normal case is what distinguishes working from broken.

Assert on parsed DOM (`container.querySelector`), not on string matching. The research probe's
false alarm came from regex-matching serialized HTML, where inert text content reads identically
to a live attribute.

Integration: 17. `ChatPane` renders an assistant answer containing markdown as formatted output.

### CP2 — reading column + parchment

18. The feed applies the parchment ground class.
19. The feed constrains content to a reading column rather than full viewport width.

Mostly visual; assert applied classes/structure and lean on the live demo for the judgment.

### CP3 — autoscroll

> **jsdom does no layout.** `scrollTop`/`scrollHeight`/`clientHeight` are all `0` unless
> explicitly defined on the element. Tests must stub that geometry or they pass vacuously —
> asserting "scrolled to bottom" when both sides are `0` proves nothing.

20. New message while at bottom → feed scrolls to bottom
21. New message while scrolled up → feed does **not** scroll
22. Jump-to-latest is hidden at the bottom
23. Jump-to-latest appears when scrolled away
24. Activating it returns to the bottom and hides the control
25. The at-bottom threshold is asserted explicitly (a value, pinned — not left to feel)

### CP4 — auto-grow composer

26. `autoGrow` grows the textarea as content grows
27. Growth caps at a max height, then scrolls internally
28. **Default off** — an existing `TextField` with `rows={n}` is unaffected (regression guard for
    every other consumer)
29. Enter still sends (regression)
30. Shift+Enter still inserts a newline (regression)

### CP5 — typing indicator

31. Pending state renders the dot row
32. Pending state still announces an accessible status
33. Animation suppressed under `prefers-reduced-motion`

## Files expected to change

- **new** `ui/src/components/Markdown.tsx` + `.css` + `.test.tsx`
- `ui/package.json` / `bun.lock` — `marked`, `dompurify` (+ `@types/dompurify` if needed)
- `ui/src/shell/ChatPane.tsx` — render markdown; scroll container + jump-to-latest; composer wiring; dots
- `ui/src/shell/ChatPane.css` — feed column, parchment, markdown-in-bubble styles, dots, jump control
- `ui/src/ds/TextField.tsx` / `.css` / `.test.tsx` — `autoGrow`
- `ui/src/shell/ChatPane.test.tsx` — integration coverage

Backend: **none**. This phase is UI-only.

## Demo checkpoints

| CP | Command | What to look for |
|----|---------|------------------|
| 1 | `bun run dev` → ask a spell question | Answer renders with real headings/lists/bold instead of one raw block |
| 2 | same | Prose sits in a centered column on parchment, not edge-to-edge |
| 3 | same, send several messages | Feed follows the newest; scroll up mid-answer and it stops following, offering jump-to-latest |
| 4 | type a long multi-line prompt | Composer grows instead of scrolling in a 2-row box; Enter still sends |
| 5 | send anything | Animated dots while thinking |

## Risks and how they are handled

- **We now own sanitization.** Pinned by rows 8–16, asserted on parsed DOM. The live risk is a
  future contributor adding `ALLOWED_*` config or bypassing the module; the tests are the guard.
- **Autoscroll fighting the reader.** Rows 21/23 exist specifically to make "don't scroll a user
  who scrolled away" a tested behavior, not an intention.
- **Silent a11y regression at CP5.** Row 32 keeps the announcement that the dots are replacing.
- **`ds/TextField` blast radius.** Row 28 makes "default off changes nothing" explicit, since
  every other field in the app shares that component.
- **DS contract drift.** CP4 is a deliberate, documented deviation *plus* an upstream proposal —
  drift with a path back, not drift by accident.
