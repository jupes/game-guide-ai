import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { Markdown } from './Markdown'

// ── pp6q.1.1 — sanitized markdown rendering ──────────────────────────────────
// Assertions run against the PARSED DOM, never against serialized HTML strings.
// During research a regex over serialized output reported a false XSS leak:
// inert text content (`<p>![x](x" onerror="alert(1))</p>`) reads identically to
// a live attribute when you match strings. querySelector cannot make that
// mistake.

function md(source: string): HTMLElement {
  const { container } = render(<Markdown source={source} />)
  return container
}

describe('Markdown — rendering', () => {
  it('renders bold as <strong> (tracer bullet: the real marked+DOMPurify pipeline runs)', () => {
    expect(md('A **bright streak** flashes.').querySelector('strong')?.textContent)
      .toBe('bright streak')
  })

  it('renders headings', () => {
    expect(md('## Fireball').querySelector('h2')?.textContent).toBe('Fireball')
  })

  it('renders unordered lists', () => {
    const items = md('- Dex save\n- Half on success').querySelectorAll('li')
    expect([...items].map((li) => li.textContent)).toEqual(['Dex save', 'Half on success'])
  })

  it('renders GFM tables without needing a plugin', () => {
    const table = md('| Level | Damage |\n|---|---|\n| 3 | 8d6 |').querySelector('table')
    expect(table).not.toBeNull()
    expect([...table!.querySelectorAll('th')].map((c) => c.textContent)).toEqual(['Level', 'Damage'])
    expect([...table!.querySelectorAll('td')].map((c) => c.textContent)).toEqual(['3', '8d6'])
  })

  it('renders fenced code blocks as <pre><code>', () => {
    const pre = md('```\n1d20 + 5\n```').querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre!.querySelector('code')?.textContent).toContain('1d20 + 5')
  })

  it('renders inline code', () => {
    expect(md('roll `1d20` now').querySelector('code')?.textContent).toBe('1d20')
  })

  it('leaves inline [n] citation markers as visible text', () => {
    // They correspond to SourceList ordering; markdown must not eat or relink them.
    expect(md('Petrifies with its gaze [1], per the bestiary [2].').textContent)
      .toContain('[1]')
  })
})

// ── Sanitization: the research-verified vector list, one test each ────────────
// These are the reason this module exists as a seam rather than an inline
// marked() call. Do not relax one without replacing the coverage.

describe('Markdown — sanitization', () => {
  it('strips <script> entirely', () => {
    const c = md("text\n\n<script>alert('xss')</script>")
    expect(c.querySelector('script')).toBeNull()
  })

  it('strips inline event handlers from images', () => {
    const c = md('<img src=x onerror="alert(1)">')
    expect(c.querySelector('img')?.getAttribute('onerror')).toBeNull()
  })

  it('drops a javascript: href from a markdown link', () => {
    const a = md('[click](javascript:alert(1))').querySelector('a')
    expect(a?.getAttribute('href') ?? null).toBeNull()
  })

  it('drops a case-mixed JaVaScRiPt: href (matching must not be case-sensitive)', () => {
    const a = md('[click](JaVaScRiPt:alert(1))').querySelector('a')
    expect(a?.getAttribute('href') ?? null).toBeNull()
  })

  it('drops a data:text/html href', () => {
    const a = md('[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)')
      .querySelector('a')
    expect(a?.getAttribute('href') ?? null).toBeNull()
  })

  it('drops a javascript: href from a raw <a> tag', () => {
    const a = md('<a href="javascript:alert(1)">c</a>').querySelector('a')
    expect(a?.getAttribute('href') ?? null).toBeNull()
  })

  it('strips <svg onload>', () => {
    expect(md('<svg onload="alert(1)"></svg>').querySelector('svg')?.getAttribute('onload'))
      .toBeNull()
  })

  it('removes <iframe>', () => {
    expect(md('<iframe src="https://evil.example"></iframe>').querySelector('iframe')).toBeNull()
  })

  it('PRESERVES a normal https link', () => {
    // Not optional. A sanitizer that strips everything passes every test above
    // while being completely broken; this is what distinguishes the two.
    const a = md('[docs](https://example.com/page)').querySelector('a')
    expect(a?.getAttribute('href')).toBe('https://example.com/page')
    expect(a?.textContent).toBe('docs')
  })
})

// ── X-10 — no remote subresources (1kg.3.2) ──────────────────────────────────
// AE-66: an answer carrying a remote markdown image, a <video src> and a style
// with url(), all pointing at example.test, must send nothing there. The flag
// is opt-in, so the first test pins that today's channels are unchanged.

function restricted(source: string): HTMLElement {
  const { container } = render(<Markdown source={source} noRemoteSubresources />)
  return container
}

describe('Markdown — X-10, no remote subresources', () => {
  it('leaves chat behaviour alone: without the flag a remote image still renders', () => {
    // The hole this flag closes. If DOMPurify ever starts dropping remote
    // images by itself, this test fails and the flag can go.
    expect(md('![sigil](https://example.test/pixel.png)').querySelector('img')).not.toBeNull()
  })

  it('drops a remote markdown image', () => {
    const c = restricted('Before ![sigil](https://example.test/pixel.png) after')
    expect(c.querySelector('img')).toBeNull()
    expect(c.textContent).toContain('Before')
  })

  it('drops a protocol-relative image, whose origin is the remote host', () => {
    expect(restricted('![x](//example.test/pixel.png)').querySelector('img')).toBeNull()
  })

  it('drops a data: image', () => {
    const source = '![x](data:image/gif;base64,R0lGODlhAQABAAAAACw=)'
    expect(restricted(source).querySelector('img')).toBeNull()
  })

  it('KEEPS a reference to a campaign asset this origin serves', () => {
    // The other half of the rule: a portrait the service serves must survive,
    // or the restriction has simply broken images (MS-7's GM asset route).
    const img = restricted('![Ondrey](/campaigns/cmp_1/assets/ast_1)').querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('/campaigns/cmp_1/assets/ast_1')
    expect(img!.getAttribute('alt')).toBe('Ondrey')
  })

  it.each([
    ['any other same-origin path', '/auth/logout'],
    ['a relative path', 'assets/ast_1.png'],
    ['the built bundle', '/assets/index.js'],
    ['an asset reference with a query, which could carry text out', '/campaigns/c/assets/a?d=the-secret'],
    ['an asset reference with a fragment', '/campaigns/c/assets/a#the-secret'],
    ['a deeper path under the asset route', '/campaigns/c/assets/a/../../../auth/logout'],
  ])('drops %s', (_name, src) => {
    // Same origin alone is too wide: every GET the service answers would be
    // something a steered model could make the GM's browser request.
    expect(restricted(`![x](${src})`).querySelector('img')).toBeNull()
  })

  it('strips in a document that cannot fetch, never in the live one', () => {
    // An <img> begins loading as soon as it exists in a document with a browsing
    // context, attached or not. jsdom fetches nothing, so this pins the mechanism:
    // the sanitizer hands back a string, and the DOM pass runs on an inert document.
    const inert = vi.spyOn(document.implementation, 'createHTMLDocument')
    const created = vi.spyOn(document, 'createElement')
    restricted('![a](https://example.test/a.png)')
    expect(inert).toHaveBeenCalledTimes(1)
    const images = created.mock.calls.filter((call) => String(call[0]).toLowerCase() === 'img')
    expect(images).toEqual([])
    inert.mockRestore()
    created.mockRestore()
  })

  it('drops a javascript: link', () => {
    expect(restricted('[click](javascript:alert(1))').querySelector('a')?.getAttribute('href') ?? null)
      .toBeNull()
  })

  it('KEEPS an ordinary https link — a link is not a subresource', () => {
    expect(restricted('[docs](https://example.com/page)').querySelector('a')?.getAttribute('href'))
      .toBe('https://example.com/page')
  })

  it('removes a <video src> and its <source>', () => {
    const c = restricted('<video src="https://example.test/clip.mp4"><source src="https://example.test/clip.webm"></video>')
    expect(c.querySelector('video')).toBeNull()
    expect(c.querySelector('source')).toBeNull()
  })

  it('removes a style attribute carrying url()', () => {
    const c = restricted('<p style="background-image: url(https://example.test/p.png)">boo</p>')
    expect(c.querySelector('p')?.getAttribute('style') ?? null).toBeNull()
    expect(c.textContent).toContain('boo')
  })

  it('keeps a style attribute that fetches nothing', () => {
    expect(restricted('<p style="font-weight: 700">bold</p>').querySelector('p')?.getAttribute('style'))
      .toContain('font-weight')
  })

  it('drops an image whose src is not a URL at all', () => {
    // `new URL()` throws on this; a reference nobody can resolve is not
    // same-origin, so the image goes.
    expect(restricted('<img src="http://[">').querySelector('img')).toBeNull()
  })

  it('removes srcset and poster even where the element survives', () => {
    const c = restricted('<img src="/campaigns/c/assets/a" srcset="https://example.test/2x.png 2x">')
    expect(c.querySelector('img')?.hasAttribute('srcset')).toBe(false)
  })

  it('leaves nothing whose src, srcset, poster or style could reach the remote host', () => {
    // The AE-66 sweep, asserted over the whole rendered tree at once.
    const c = restricted(
      '![a](https://example.test/a.png)\n\n'
      + '<video src="https://example.test/v.mp4" poster="https://example.test/p.png"></video>\n\n'
      + '<p style="background: url(https://example.test/b.png)">x</p>\n\n'
      + '<iframe src="https://example.test/f"></iframe>\n\n'
      + '<link rel="stylesheet" href="https://example.test/s.css">',
    )
    for (const element of c.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain('example.test')
      }
    }
  })
})
