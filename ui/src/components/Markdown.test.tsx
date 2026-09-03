import { describe, it, expect } from 'vitest'
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
