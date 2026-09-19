/**
 * AssistantDocumentLink — CANVAS-3 (1kg.3.2).
 *
 * The record's sentence is short and every word of it is load-bearing: the row
 * "reads `<kind> · saved to <category>` with the action **Open in canvas**".
 * It says *saved to*, because X-6 has already stored the document; it names the
 * action, because the canvas is a place the GM chooses to go (AE-16); and it is
 * a lane row rather than a swap, because the canvas never swaps by itself.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { DocumentLink } from './contracts'
import { AssistantDocumentLink } from './AssistantDocumentLink'
import { DOCUMENT_LINK } from './laneFixtures'

const link = DOCUMENT_LINK as unknown as DocumentLink

describe('AssistantDocumentLink', () => {
  it('is a real button whose accessible name names the action', () => {
    render(<AssistantDocumentLink link={link} onOpen={vi.fn()} />)
    const row = screen.getByRole('button', { name: /Open in canvas/ })
    expect(row.tagName).toBe('BUTTON')
    expect(row).toHaveAttribute('type', 'button')
  })

  it('reads `<kind> · saved to <category>`', () => {
    render(<AssistantDocumentLink link={link} onOpen={vi.fn()} />)
    expect(screen.getByText('NPC Dossier · saved to NPCs')).toBeInTheDocument()
  })

  it('shows the document title', () => {
    render(<AssistantDocumentLink link={link} onOpen={vi.fn()} />)
    expect(screen.getByText('Ondrey the Ferryman')).toBeInTheDocument()
  })

  it('opens NOTHING on render — the canvas is entered deliberately', () => {
    const onOpen = vi.fn()
    render(<AssistantDocumentLink link={link} onOpen={onOpen} />)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('hands the whole link back on click, so the caller need not re-look it up', async () => {
    const onOpen = vi.fn()
    render(<AssistantDocumentLink link={link} onOpen={onOpen} />)
    await userEvent.click(screen.getByRole('button', { name: /Open in canvas/ }))
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(link)
  })

  it('opens from the keyboard, because it is a button and not a div', async () => {
    const onOpen = vi.fn()
    render(<AssistantDocumentLink link={link} onOpen={onOpen} />)
    screen.getByRole('button', { name: /Open in canvas/ }).focus()
    await userEvent.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('hides its icons from assistive technology', () => {
    const { container } = render(<AssistantDocumentLink link={link} onOpen={vi.fn()} />)
    const icons = container.querySelectorAll('.material-symbols-rounded')
    expect(icons.length).toBeGreaterThan(0)
    for (const icon of icons) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it('never writes the title into an id, a class or a data attribute (X-7)', () => {
    const { container } = render(<AssistantDocumentLink link={link} onOpen={vi.fn()} />)
    for (const element of container.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain('Ondrey')
      }
    }
  })

  it('renders nothing for a document type this bundle does not know (X-8)', () => {
    const stranger = { ...DOCUMENT_LINK, type: 'grimoire' } as unknown as DocumentLink
    const { container } = render(<AssistantDocumentLink link={stranger} onOpen={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('names the right category for a session-notes document', () => {
    const notes = {
      document_id: 'doc_1',
      type: 'session-notes',
      title: 'Session 12',
      library_category: 'session-log',
    } as unknown as DocumentLink
    render(<AssistantDocumentLink link={notes} onOpen={vi.fn()} />)
    expect(screen.getByText('Session Notes · saved to Session log')).toBeInTheDocument()
  })
})
