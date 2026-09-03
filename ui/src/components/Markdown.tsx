/**
 * Markdown — renders model-authored markdown as sanitized HTML (pp6q.1.1).
 *
 * Answers are LLM output, so this is a genuine XSS surface: the model can be
 * steered into emitting a <script> tag or a javascript: link by hostile corpus
 * text or a crafted prompt. Everything rendered here goes through DOMPurify
 * first, and the vector list in Markdown.test.tsx is the regression suite that
 * keeps it that way.
 *
 * Why `marked` + `dompurify` (3 packages) over `react-markdown` + `remark-gfm`
 * (95, measured): ui/ has three runtime dependencies by design and the repo
 * carries an advisory backlog, so 95 transitive packages for prose formatting
 * is a poor trade. The cost is that we render an HTML string, which means
 * `dangerouslySetInnerHTML` over sanitized output — the standard, audited path
 * rather than a hand-rolled one, but it does make sanitization *our*
 * correctness obligation. Hence the tests.
 *
 * If a later phase needs per-node React components (e.g. turning inline [1]
 * citations into controls that scroll to SourceList), that is the one thing
 * `react-markdown` buys that CSS cannot — revisit then, not before.
 */

import * as React from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import './Markdown.css'

export interface MarkdownProps {
  /** Markdown source. Treated as untrusted. */
  source: string
  className?: string
}

/**
 * Parse + sanitize. `marked` ships GFM (tables, strikethrough) with no plugin,
 * so nothing extra is configured here.
 *
 * `async: false` pins the synchronous overload — marked's return type is
 * `string | Promise<string>` and rendering needs the string.
 *
 * Deliberately NOT exported: a non-component export here breaks React Fast
 * Refresh (`react-refresh/only-export-components`), and nothing outside needs
 * it — the tests exercise the real pipeline through <Markdown/>. If a caller
 * ever does need the pure function, move it to its own module rather than
 * re-exporting it from here.
 */
function renderMarkdown(source: string): string {
  const raw = marked.parse(source, { async: false, gfm: true, breaks: true })
  return DOMPurify.sanitize(raw)
}

export function Markdown({ source, className }: MarkdownProps): React.JSX.Element {
  // Recomputed only when the source changes: sanitizing is not free and an
  // assistant answer re-renders on every unrelated ChatPane state change.
  const html = React.useMemo(() => renderMarkdown(source), [source])
  return (
    <div
      className={['aether-markdown', className].filter(Boolean).join(' ')}
      // Safe by construction: `html` is DOMPurify output, never raw model text.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
