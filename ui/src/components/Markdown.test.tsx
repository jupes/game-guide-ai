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

  it('strips inline event handlers from an image that SURVIVES', () => {
    // The vector has to be one that survives (va8). For a dropped image
    // `querySelector('img')?.getAttribute('onerror')` is `undefined`, and
    // coercing that to null would make the assertion pass *because the element
    // is gone* — a test about onerror that no longer touches onerror.
    const img = md('<img src="/campaigns/c/assets/a" alt="a" onerror="alert(1)">').querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('onerror')).toBeNull()
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

  it('removes <svg> entirely — onload and all', () => {
    // Stronger, and true: the whole element goes (va8). Asserting that its
    // onload attribute is null would now pass because there is no <svg> to
    // carry one.
    expect(md('<svg onload="alert(1)"></svg>').querySelector('svg')).toBeNull()
  })

  it('strips an inline event handler from an element that SURVIVES', () => {
    // Keeps DOMPurify's event-handler stripping covered with a vector this
    // component does not delete.
    const b = md('<b onmouseover="alert(1)">hover</b>').querySelector('b')
    expect(b).not.toBeNull()
    expect(b!.textContent).toBe('hover')
    expect(b!.getAttribute('onmouseover')).toBeNull()
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

// ── X-10 — no remote subresources, in EVERY channel (1kg.3.2, va8) ───────────
// AE-66: an answer carrying a remote markdown image, a <video src> and a style
// with url(), all pointing at example.test, must send nothing there. Stripping
// used to be opt-in and off for the chat channels; va8 made it unconditional,
// so every test below that renders with no props is the ChatPane shape.
//
// What these tests prove, exactly: no element carrying a remote reference
// survives rendering. They cannot prove that no request left a browser — jsdom
// fetches no subresources. That claim belongs to ui/e2e/security.spec.ts, which
// runs a real Chromium.

// `noRemoteSubresources` is now accepted and ignored, so this helper renders
// exactly what `md()` does. That is deliberate and worth keeping: every test
// below that uses it now proves that passing the prop CHANGES NOTHING — the
// chat path and the Workbench path are byte-identical.
function restricted(source: string): HTMLElement {
  const { container } = render(<Markdown source={source} noRemoteSubresources />)
  return container
}

/**
 * The AE-66 fixture: everything an answer might carry, at once.
 *
 * It opens with content that must SURVIVE — a campaign-asset portrait, an
 * ordinary https link and a marker string — because the sweep over it is an
 * assertion about absence, and absence is satisfied by an empty page.
 *
 * The last eight entries are PINS OF DOMPURIFY'S OWN DEFAULTS, not vectors this
 * fix closed: `<iframe>`, `<object data>`, `<embed>`, `<link rel=stylesheet>`,
 * `<base>`, `<meta http-equiv=refresh>`, `<a ping>` and `lowsrc` are already
 * gone before this component's code runs. They ride here so that a dependency
 * upgrade which stopped dropping one of them fails a test.
 */
const AE66_FIXTURE = [
  'MARKER-KEEP, and a portrait: ![Ondrey](/campaigns/c/assets/a)',
  '[docs](https://example.com/page)',
  '![a](https://example.test/a.png)',
  '<video src="https://example.test/v.mp4" poster="https://example.test/p.png"></video>',
  '<p style="background: url(https://example.test/b.png)">x</p>',
  '<table background="https://example.test/t.png"><tr><td background="https://example.test/d.png">cell</td></tr></table>',
  '<img src="/campaigns/c/assets/a" alt="a" srcset="https://example.test/2x.png 2x" lowsrc="https://example.test/l.png">',
  '<iframe src="https://example.test/f"></iframe>',
  '<object data="https://example.test/o"></object>',
  '<embed src="https://example.test/e">',
  '<link rel="stylesheet" href="https://example.test/s.css">',
  '<base href="https://example.test/">',
  '<meta http-equiv="refresh" content="0;url=https://example.test/">',
  '<a href="/x" ping="https://example.test/p">ping</a>',
].join('\n\n')

describe('Markdown — X-10, no remote subresources', () => {
  it('drops a remote image in the chat channel too — the va8 hole, inverted', () => {
    // This test used to assert the opposite, pinning the bug: "without the flag
    // a remote image still renders". It is the single clearest statement of
    // this fix, so it is inverted rather than deleted.
    const c = md('Before ![sigil](https://example.test/pixel.png) after')
    expect(c.textContent).toContain('Before')
    expect(c.querySelector('img')).toBeNull()
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

  it('AE-66 sweep: no attribute anywhere can reach the remote host, and the page is not empty', () => {
    const c = md(AE66_FIXTURE) // no props — the ChatPane shape
    // Positive control FIRST. The sweep below is green over a tree that carries
    // no attributes at all, so prove the tree is not empty and really does
    // carry surviving, attribute-bearing content before sweeping it.
    expect(c.querySelector('img')?.getAttribute('src')).toBe('/campaigns/c/assets/a')
    expect(c.querySelector('a')?.getAttribute('href')).toBe('https://example.com/page')
    expect(c.textContent).toContain('MARKER-KEEP')
    for (const element of c.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain('example.test')
      }
    }
  })
})

// ── va8 — the vector list, rendered with NO PROPS (the ChatPane shape) ───────
// Every row here rendered a live remote reference in chat before this fix.
// Rows 14–18 were live in the Workbench too: the previous check was
// `style.includes('url(')`, and CSS unescapes `u\72l(` and `\75rl(` back into
// `url()` while `image-set()` needs no `url()` at all.

const MARKER = 'MARKER-KEEP'

describe('va8 — no channel renders a remote subresource', () => {
  it.each([
    ['1 — a remote markdown image', '![x](https://example.test/pixel.png)', 'img'],
    ['2 — a raw <img> with a remote src', '<img src="https://example.test/a.png">', 'img'],
    ['3 — a protocol-relative image, whose origin is the remote host', '![x](//example.test/a.png)', 'img'],
    ['4 — a data: image', '![x](data:image/gif;base64,R0lGODlhAQABAAAAACw=)', 'img'],
    ['7 — an <svg> carrying an <image href>', '<svg><image href="https://example.test/a.png"></image></svg>', 'svg, image'],
    ['9 — a <video> with src and poster', '<video src="https://example.test/v.mp4" poster="https://example.test/p.png"></video>', 'video'],
    ['10 — an <audio src>', '<audio src="https://example.test/a.mp3"></audio>', 'audio'],
    ['10 — a <track src> inside a <video>', '<video><track src="https://example.test/t.vtt"></video>', 'video, track'],
  ])('drops %s', (_name, vector, selector) => {
    const c = md(`${MARKER}\n\n${vector}`)
    // Positive control first: a negative assertion over a tree that never
    // rendered is vacuous.
    expect(c.textContent).toContain(MARKER)
    expect(c.querySelectorAll(selector)).toHaveLength(0)
  })

  it.each([
    ['12 — url()', '<p style="background: url(https://example.test/b.png)">boo</p>'],
    ['13 — URL(), upper case', '<p style="background: URL(https://example.test/b.png)">boo</p>'],
    ['14 — a CSS escape, u\\72l()', '<p style="background:u\\72l(https://example.test/a.png)">boo</p>'],
    ['15 — a CSS escape, \\75rl()', '<p style="background:\\75rl(https://example.test/a.png)">boo</p>'],
    ['16 — image-set()', '<p style="background-image:image-set(\'https://example.test/a.png\' 1x)">boo</p>'],
    ['17 — -webkit-image-set()', '<p style="background-image:-webkit-image-set(\'https://example.test/a.png\' 1x)">boo</p>'],
    // A backslash that fetches nothing, dropped anyway: the rule refuses an
    // escape rather than deciding what it unescapes to, which is exactly the
    // analysis that produced the bypass above.
    ['18 — a backslash that fetches nothing', '<p style="content:\'\\A\'">boo</p>'],
  ])('removes a style attribute that could fetch: %s', (_name, vector) => {
    const p = md(vector).querySelector('p')
    // The paragraph and its text survive — only the attribute goes. Without
    // these two lines a sanitiser that deleted the element would pass.
    expect(p).not.toBeNull()
    expect(p!.textContent).toBe('boo')
    expect(p!.hasAttribute('style')).toBe(false)
  })

  it('5 — keeps a campaign-asset image but drops its srcset', () => {
    const img = md('<img src="/campaigns/c/assets/a" alt="a" srcset="https://example.test/2x.png 2x">')
      .querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('/campaigns/c/assets/a')
    expect(img!.hasAttribute('srcset')).toBe(false)
  })

  it('6 — removes a <picture>\'s <source srcset> and keeps the fallback image', () => {
    const c = md('<picture><source srcset="https://example.test/a.png"><img src="/campaigns/c/assets/a" alt="a"></picture>')
    expect(c.querySelector('img')?.getAttribute('src')).toBe('/campaigns/c/assets/a')
    expect(c.querySelector('source')).toBeNull()
  })

  it('8 — keeps an <input type=image> but drops its src', () => {
    // An attribute-level drop, not an element-level one: asserting the <input>
    // is gone would fail, and it is supposed to survive.
    const input = md('<input type="image" src="https://example.test/a.png">').querySelector('input')
    expect(input).not.toBeNull()
    expect(input!.hasAttribute('src')).toBe(false)
  })

  it('11 — keeps a table and its cell but drops both background attributes', () => {
    const c = md('<table background="https://example.test/a.png"><tr><td background="https://example.test/b.png">cell</td></tr></table>')
    const table = c.querySelector('table')
    const cell = c.querySelector('td')
    expect(table).not.toBeNull()
    expect(cell?.textContent).toBe('cell')
    expect(table!.hasAttribute('background')).toBe(false)
    expect(cell!.hasAttribute('background')).toBe(false)
  })

  it('AC-4: a caller cannot turn stripping off — the type rejects it, and it would change nothing if it did', () => {
    // @ts-expect-error va8 AC-4: `noRemoteSubresources` is narrowed to the literal
    // `true`, so `false` must not compile. This directive FAILS the build if the
    // error ever stops occurring (TS2578), which is the only form of this gate
    // that can go red.
    const { container } = render(<Markdown source="![x](https://example.test/p.png)" noRemoteSubresources={false} />)
    expect(container.querySelector('img')).toBeNull()
  })
})

// ── fu9 — an image that survives always carries alt text ─────────────────────

describe('fu9 — surviving images carry alt text', () => {
  it('gives a campaign-asset image that arrived with NO alt attribute alt=""', () => {
    const img = md('<img src="/campaigns/c/assets/a">').querySelector('img')
    // It is given a caption, not deleted: a portrait the GM asked for must not
    // be destroyed for lack of one.
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('/campaigns/c/assets/a')
    expect(img!.getAttribute('alt')).toBe('')
  })

  it('every image that survives a mixed fixture carries an alt attribute', () => {
    const c = md([
      '![Ondrey](/campaigns/c/assets/one)',
      '<img src="/campaigns/c/assets/two">',
      '![](/campaigns/c/assets/three)',
      '![sigil](https://example.test/pixel.png)',
    ].join('\n\n'))
    const images = [...c.querySelectorAll('img')]
    // Positive control: "every image has alt" is true of no images at all.
    expect(images).toHaveLength(3)
    for (const image of images) expect(image.hasAttribute('alt')).toBe(true)
    expect(images.map((image) => image.getAttribute('alt'))).toEqual(['Ondrey', '', ''])
  })
})
