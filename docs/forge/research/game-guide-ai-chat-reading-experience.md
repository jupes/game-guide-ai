# Research: chat reading experience (markdown, scroll, reading column, composer)

Generated: 2026-09-03
Repo: `game-guide-ai`
Beads: `agent-forge-harness-pp6q.1` (parent epic `agent-forge-harness-pp6q`)
Phase 1 of the chat-experience overhaul. Deliberately scoped to changes that need **no**
architecture change — it does not touch the `Exchange`-pair model, which Phase 2 (`pp6q.2`)
replaces.

## Framing

Confirmed with the user: adopt ChatGPT/Claude's **interaction mechanics**, keep Aetheril's
**visual identity** (parchment ground, display face, the D&D card widgets). Where they conflict,
mechanics win for behavior, Aetheril wins for appearance.

## What the code already answers

Explored `ui/src/shell/ChatPane.tsx`, `ChatPane.css`, `ui/src/ds/{ChatMessage,TextField}.tsx`,
`ui/package.json`, and `repos/aetheril-design-system/source/ui_kits/aetheril-app/ChatView.jsx`.

| Question | Answer (verified, not assumed) |
|---|---|
| Is anything rendering markdown today? | **No.** `ChatPane.tsx:273` renders `{exchange.response.answer}` as a raw string. No markdown/remark/marked/DOMPurify anywhere in `ui/`. |
| Does the feed autoscroll? | **No.** `ChatPane.tsx` has no scroll handling at all. `.chat-pane__exchanges` is `overflow: hidden auto` so it *can* scroll; nothing ever scrolls it. The DS mock does (`ChatView.jsx`: `feedRef.current.scrollTop = feedRef.current.scrollHeight`). |
| Is there a reading column? | **No.** `ChatPane.css:58`'s `max-width: 44rem` is on `.chat-pane__suggestions` only. The feed is unconstrained. The mock uses a 760px centered column. |
| Is the parchment ground applied to the feed? | **No.** `.aether-parchment` exists (`ds/tokens/base.css:23`, mirrored from the DS) and the mock applies it to the feed; `ChatPane` never does. |
| Can `ChatMessage` host rich content? | **Yes.** `children?: React.ReactNode` — `ChatPane` already passes elements (`<span role="status">`). No component change needed to host rendered markdown. |
| Does `TextField` auto-grow? | **No.** It renders `<textarea rows={rows}>`; `ChatPane` passes `rows={2}`, fixed. |
| Does the DS contract have `autoGrow`? | **No.** `aetheril-design-system/source/components/forms/TextField.d.ts` has no such prop. |
| Is DS prop-parity enforced? | **No automated gate.** Five `ds/` components carry a "mirrors the DS d.ts exactly" *comment*; no test asserts it. Deviating is documentable, not a build failure. |
| Current runtime deps | **3**: react 19.2, react-dom, zod. Vite 8. The repo also has 17 open dependabot advisories, so dependency surface is a live concern. |
| Is Enter-to-send already right? | **Yes.** `handleKeyDown` sends on Enter, newline on Shift+Enter. Must not regress. |

### Things that look adjacent but are unaffected

- `parseDiceNotation(exchange.response.answer)` reads the **raw** answer string — markdown
  rendering does not change its input.
- `exportChat(exchanges)` likewise exports raw answers.
- The inline `[1]`/`[2]` citation markers are plain text in the answer and correspond to
  `SourceList` ordering. Markdown rendering must leave them as visible text; making them
  *interactive* is explicitly out of scope here (noted as a Phase 4+ possibility).

## Decisions taken (user-confirmed 2026-09-03)

### D1 — markdown via `marked` + `dompurify`, not `react-markdown`

Measured, not estimated:

| Option | Packages installed |
|---|---|
| `react-markdown` + `remark-gfm` | **95** |
| `marked` + `dompurify` | **3** |

Chosen: **`marked` + `dompurify`**. `ui/` has three runtime dependencies by design and the repo
carries an existing advisory backlog; 95 transitive packages for prose formatting is a poor trade.
`marked` ships GFM (tables, strikethrough) with no plugin.

The cost is that this path renders an HTML string, so it needs `dangerouslySetInnerHTML` over
DOMPurify-sanitized output. That is the industry-standard sanitizer, not a hand-rolled one — but
it makes sanitization *our* correctness obligation, so it must be pinned by tests (see below).

Revisit only if a later phase genuinely needs per-node React components (e.g. turning `[1]` into a
clickable citation) — that is the one capability `react-markdown` buys that CSS cannot.

### D2 — no syntax highlighting

Style `<pre>`/`<code>` with the existing `--aether-font-mono` token. A D&D rules assistant rarely
emits source code, and highlighters (shiki/prism/highlight.js) would dwarf the markdown renderer
they decorate.

### D3 — `autoGrow` goes on `ds/TextField`, as a documented deviation

A growing textarea belongs to the field component, not to the one screen that wants it. Since no
test enforces DS parity, this is a documentable deviation rather than a break. **Also propose it
upstream** to `aetheril-design-system` so the contract converges instead of silently drifting —
this epic is about *reducing* drift, so adding some without a path back would be self-defeating.

## Verified: the sanitizer actually holds

Probed `marked@16` + `dompurify@3.4` under jsdom (the same environment vitest uses), because
DOMPurify needs a DOM and that was worth confirming rather than assuming.

Rendering — all correct out of the box:

- GFM tables → `<table>`; fenced code → `<pre><code>`; headings, `<strong>`, lists, inline `<code>`.

XSS vectors — every one neutralised:

| Vector | Result |
|---|---|
| `<script>alert('xss')</script>` | removed |
| `<img src=x onerror=...>` | `<img src="x">`, handler stripped |
| `[click](javascript:alert(1))` | `<a>click</a>` — href dropped |
| `[click](JaVaScRiPt:alert(1))` (case-mixed) | href dropped |
| `[click](data:text/html;base64,...)` | href dropped |
| `<a href="javascript:...">` (raw HTML) | href dropped |
| `<svg onload=...>` | handler stripped |
| `<iframe src=...>` | removed entirely |
| `[docs](https://example.com/page)` | **preserved** — normal links still work |

> A first pass at this probe reported a false leak on `javascript:` because `marked` had swallowed
> the link into a raw-HTML block, so it never became a link at all. Retested in isolation. Worth
> recording: a sanitizer test that doesn't verify the *normal* case still works, and doesn't
> isolate each vector, can report either false safety or false alarm.

**These exact vectors become the regression suite.** Sanitization we own is sanitization we pin.

## Open questions the code cannot answer

None blocking. Two judgment calls deferred to the plan phase:

1. **Scroll threshold** — how close to the bottom counts as "following". A few px is too strict
   (fails on sub-pixel/zoom); too loose and it yanks the view while the user reads. Pick a value
   and pin it in a test rather than leaving it to feel.
2. **Where rendered markdown lives** — a `ds/` component (reusable, but the DS has no such
   component to mirror) versus a local `components/` module. Leaning local: it is app glue over a
   third-party renderer, not a design-system primitive.

## Risks

- **Sanitization is now our responsibility.** Mitigated by D1's test suite; the risk is a future
  contributor adding `ALLOWED_*` config or swapping in raw HTML without re-running those tests.
- **Autoscroll fighting the reader** is the classic failure of this feature. Follow only when
  already at the bottom; never scroll a user who has scrolled away.
- **Losing the accessible pending announcement.** Today's `role="status"` "Consulting the tomes…"
  must not be replaced by a purely visual dot animation — the dots are decoration, the
  announcement is the actual affordance for assistive tech.
