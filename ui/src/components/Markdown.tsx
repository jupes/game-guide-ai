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
 * an exfiltration channel for a model steered by hostile text. So the X-10 pass
 * below runs on EVERY render, in every channel — chat and Workbench alike
 * (agent-forge-harness-va8; it was opt-in, and off for chat, until this fix).
 * Nothing rendered here may fetch anything by itself:
 *
 *   - the one surviving image is a reference to a campaign asset this origin
 *     serves (MS-7), and it always carries an alt attribute (fu9);
 *   - every other subresource element and attribute is dropped;
 *   - a `style` attribute is dropped when its value could fetch — see
 *     `styleMayFetch`;
 *   - links are untouched: a link is a navigation the reader chooses, not a
 *     subresource the page loads.
 *
 * This is the first of two layers, and it is the one that can be unit-tested:
 * jsdom fetches nothing, so these tests prove that no element carrying a remote
 * reference survives rendering, never that no request left a browser. The
 * second layer is the Content-Security-Policy, which both hosts send —
 * `service/security_headers.py` and `ui/nginx.conf` — and which is proven in a
 * real browser by `ui/e2e/security.spec.ts`.
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
   * Accepted and ignored. Stripping is unconditional (va8); the type is
   * narrowed to the literal `true` so that `noRemoteSubresources={false}` is a
   * compile error rather than a silent opt-out. Kept so the Workbench call
   * sites that pass it still compile.
   */
  noRemoteSubresources?: true
}

/**
 * Elements that fetch something of their own accord — plus `template`, which
 * fetches nothing itself but hides what it wraps from every sweep below (its
 * `.content` is a separate DocumentFragment) while `innerHTML` still serializes
 * it. `marked` never emits one, so only raw HTML in model output loses anything
 * (agent-forge-harness-1q7).
 */
const SUBRESOURCE_ELEMENTS =
  'iframe, frame, frameset, object, embed, video, audio, source, track, link, style, svg, portal, template'

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

/** CSS functions that fetch. `image-set(` also covers `-webkit-image-set(`. */
const FETCHING_CSS_FUNCTIONS = ['url(', 'image-set(', 'image(', 'cross-fade(', 'src(']

/**
 * True when a `style` attribute value could fetch something.
 *
 * A `style` is dropped when its value, lower-cased, contains a backslash or one
 * of the fetching functions above; otherwise it is kept unchanged.
 *
 * The backslash rule is deliberately broad, and it is the point of this
 * function. CSS unescapes `u\72l(` and `\75rl(` back into `url(`, so the
 * previous `includes('url(')` check missed both — measured against
 * dompurify@3.4.14, and the Workbench was exposed to it too, not only the chat
 * channels. Deciding what a given escape unescapes to is exactly the analysis
 * that produced the bypass, so a value carrying an escape is refused WITHOUT
 * being interpreted. A style that merely contains a backslash and fetches
 * nothing (`content:'\A'`) is refused with it, and nothing legitimate is lost:
 * `marked` emits no `style` attribute at all — GFM table alignment comes out as
 * `align="left"` — so only raw HTML in model output can reach this.
 *
 * Normalizing through the CSSOM instead was considered and rejected: jsdom's
 * CSS parser and Chromium's disagree about unknown functions, and a test that
 * passes in jsdom because jsdom dropped the value proves nothing about a
 * browser.
 */
function styleMayFetch(value: string): boolean {
  const lowered = value.toLowerCase()
  return lowered.includes('\\') || FETCHING_CSS_FUNCTIONS.some((name) => lowered.includes(name))
}

/**
 * X-10, applied to already-sanitized DOM — in a document that cannot fetch
 * (see `renderMarkdown`).
 *
 * `host` is `ParentNode` rather than `HTMLElement` because this function calls
 * itself on a `<template>`'s `.content` — a `DocumentFragment`, never an
 * `HTMLElement` — see the recursion below.
 */
function stripRemoteSubresources(host: ParentNode): void {
  // A <template>'s `.content` is a SEPARATE DocumentFragment, not a descendant
  // in the tree the sweeps below walk — that is exactly why none of them ever
  // see what a template wraps, while `Element.innerHTML` still re-serializes it
  // into the STRING handed to `dangerouslySetInnerHTML`. The template itself is
  // removed by the first sweep (it is in SUBRESOURCE_ELEMENTS). This runs FIRST,
  // before that removal, so every sweep has already cleaned what the template
  // wraps: defence in depth, so the day the removal is relaxed nothing it
  // wrapped is live. Recursion reaches nested templates without a separate loop.
  for (const template of host.querySelectorAll('template')) {
    stripRemoteSubresources(template.content)
  }
  for (const element of host.querySelectorAll(SUBRESOURCE_ELEMENTS)) element.remove()
  for (const element of host.querySelectorAll('*')) {
    for (const name of SUBRESOURCE_ATTRIBUTES) element.removeAttribute(name)
    if (element.tagName !== 'IMG') element.removeAttribute('src')
    const style = element.getAttribute('style')
    if (style !== null && styleMayFetch(style)) element.removeAttribute('style')
  }
  // An image is kept only when it points at a campaign asset this origin serves;
  // there is no half-measure, because an <img> with a stripped src is a broken icon.
  for (const image of host.querySelectorAll('img')) {
    if (!isAssetReference(image.getAttribute('src'))) {
      image.remove()
      continue
    }
    // fu9: an image that survives always carries alt text. DOMPurify trims every
    // attribute value, so `alt="   "` arrives here as `alt=""` and the only
    // reachable alt-less case is a MISSING attribute — hence one condition and
    // no trim guard, which would be a branch that cannot be taken. `marked`
    // already emits `alt=""` for `![](…)`, so raw HTML is normalised to the same
    // thing, and a portrait the GM asked for is not destroyed for lack of a
    // caption.
    if (!image.hasAttribute('alt')) image.setAttribute('alt', '')
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
function renderMarkdown(source: string): string {
  const raw = marked.parse(source, { async: false, gfm: true, breaks: true })
  const clean = DOMPurify.sanitize(raw)
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

// `noRemoteSubresources` is deliberately NOT destructured: it is accepted and
// ignored, and `noUnusedLocals` would fail the build on an unread binding.
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
