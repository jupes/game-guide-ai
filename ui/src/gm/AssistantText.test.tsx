/**
 * AssistantText — lane prose (1kg.3.2, RAIL-17 + X-10).
 *
 * Two obligations: the text is Markdown rendered through the sanitizer, and it
 * can never fetch anything off-origin. The second is the reason this component
 * takes a string instead of children — a `children` prop would let a caller put
 * raw model output on the page without passing this way at all.
 */

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { AssistantText } from './AssistantText'

function prose(source: string): HTMLElement {
  const { container } = render(<AssistantText source={source} />)
  return container
}

describe('AssistantText', () => {
  it('renders markdown through the shared sanitizer', () => {
    expect(prose('grapple **pressure**, not raw damage').querySelector('strong')?.textContent)
      .toBe('pressure')
  })

  it('carries both the markdown block styles and the lane type class', () => {
    const root = prose('a line').firstElementChild
    expect(root).toHaveClass('aether-markdown')
    expect(root).toHaveClass('assistant-text')
  })

  it('accepts an extra class without dropping its own', () => {
    const { container } = render(<AssistantText source="x" className="extra" />)
    expect(container.firstElementChild).toHaveClass('assistant-text', 'extra')
  })

  it('turns X-10 on without being asked: a remote image never renders', () => {
    // The lane is the surface the record names; forgetting the flag here would
    // reopen the ![](https://host/?d=<secret>) channel for every lane at once.
    expect(prose('![sigil](https://example.test/pixel.png)').querySelector('img')).toBeNull()
  })

  it('keeps a same-origin asset reference', () => {
    expect(prose('![Ondrey](/workbench/assets/ast_1.png)').querySelector('img')).not.toBeNull()
  })

  it('still strips script, as the sanitizer always did', () => {
    expect(prose("hi\n\n<script>alert('x')</script>").querySelector('script')).toBeNull()
  })
})
