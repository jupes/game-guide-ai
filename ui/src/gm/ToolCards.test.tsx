import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ToolCardBody } from './ToolCards'
import { TOOL_CARD_FIXTURES } from './laneFixtures'
import type { CardContent } from './contracts'
import { toolById } from './registry'

describe('compact Workbench card renderers', () => {
  it.each([
    ['loot', 'The offering box', 'UL', 2],
    ['names', 'Names for the coast', 'UL', 2],
    ['hooks', 'Riverside hooks', 'OL', 1],
  ] as const)('renders %s with a compact card, registry metadata, and a real list', (kind, title, tag, count) => {
    const { container } = render(<ToolCardBody card={TOOL_CARD_FIXTURES[kind]} />)
    const meta = toolById(kind)!
    const group = screen.getByRole('group', { name: meta.label })
    expect(within(group).getByRole('heading', { name: title, level: 3 })).toBeInTheDocument()
    expect(within(group).getByRole('list').tagName).toBe(tag)
    expect(within(group).getAllByRole('listitem')).toHaveLength(count)
    expect(container.querySelector('.game-content-card--compact')).toBeInTheDocument()
    const icon = container.querySelector('.game-content-card__kicker-icon')
    expect(icon).toHaveTextContent(meta.icon)
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(within(group).queryByRole('button')).toBeNull()
    expect(within(group).queryByRole('link')).toBeNull()
  })

  it('renders loot quantity, value, and note in order and omits absent fields', () => {
    render(<ToolCardBody card={TOOL_CARD_FIXTURES.loot} />)
    const [first, second] = screen.getAllByRole('listitem')
    expect(first).toHaveTextContent('Tarnished silver censer ×2 · 30 gpSmells of cedar.')
    expect(second).toHaveTextContent(/^Wax seal$/)
  })

  it('uses the original rules citation numbers in the expandable source list', async () => {
    render(<ToolCardBody card={TOOL_CARD_FIXTURES.rules} />)
    expect(screen.getByText(/Take half cover \[2\]/)).toHaveTextContent('Check the attack [4].')
    await userEvent.click(screen.getByText('2 sources'))
    const [first, second] = screen.getAllByRole('listitem')
    expect(first).toHaveTextContent(/^\[2\] Half cover/)
    expect(second).toHaveTextContent(/^\[4\] Attacks/)
  })

  it('treats all model-facing fields and source text as inert text', async () => {
    const literal = '<img src=x onerror=example> ![x](https://example.test/image) javascript:example'
    const cards: CardContent[] = [
      { card_kind: 'loot', loot: { title: literal, items: [{ name: literal, value: literal, note: literal }] } },
      { card_kind: 'names', names: { title: literal, entries: [{ name: literal, note: literal }] } },
      { card_kind: 'hooks', hooks: { title: literal, entries: [{ title: literal, text: literal }] } },
      { card_kind: 'rules', rules: { title: literal, answer: literal, citations: [
        { n: 2, source: { book: literal, chapter: literal, section: literal, entity: literal, page: null, snippet: literal } },
      ] } },
    ]
    const { container } = render(<>{cards.map((card) => <ToolCardBody key={card.card_kind} card={card} />)}</>)
    await userEvent.click(screen.getByText('1 source'))
    expect(screen.getAllByRole('heading', { level: 3, name: literal })).toHaveLength(4)
    expect(container).toHaveTextContent(literal)
    expect(container.querySelector('img, a, script, iframe, [onerror]')).toBeNull()
  })
})
