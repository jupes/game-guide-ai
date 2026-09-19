/**
 * CanvasPane — the persistent canvas's control surface (1kg.6.1).
 *
 * Covers CANVAS-13 (one polite live region that does not re-announce),
 * CANVAS-27 (the history disclosure), CANVAS-29 (the wash acknowledgement),
 * CANVAS-32 (guarded close and where focus lands), REVEAL-13 / REVEAL-14 /
 * REVEAL-8 (what the header says about exposure, including the unknown state),
 * §10.2's three canvas-header presentations, and X-8's unknown document type.
 */

import * as React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CanvasPane, type CanvasPaneProps } from './CanvasPane'
import type { DocumentVersion } from './contracts'

const VERSIONS: DocumentVersion[] = [
  {
    number: 3,
    author: 'assistant',
    summary: 'Wants the signet',
    created_at: '2026-09-16T19:36:00Z',
    sealed: true,
    changed_fields: ['wants'],
    restored_from: null,
  },
  {
    number: 2,
    author: 'gm',
    summary: 'Voice, by hand',
    created_at: '2026-09-16T19:34:00Z',
    sealed: true,
    changed_fields: ['voice'],
    restored_from: null,
  },
]

function pane(props: Partial<CanvasPaneProps> = {}): React.JSX.Element {
  return (
    <CanvasPane title="Sister Ondrey Vashe" documentType="npc" {...props}>
      {props.children ?? <p>The dossier</p>}
    </CanvasPane>
  )
}

describe('the pane is a named landmark with a real heading', () => {
  it('names the region after the document and heads it with the title', () => {
    render(pane())
    const region = screen.getByRole('region', { name: 'Sister Ondrey Vashe' })
    expect(within(region).getByRole('heading', { level: 2, name: 'Sister Ondrey Vashe' })).toBeInTheDocument()
  })

  it('scrolls the body and keeps it reachable from the keyboard', () => {
    const { container } = render(pane({ children: <p>The dossier</p> }))
    const body = container.querySelector('.gm-canvas__body')
    expect(body).toHaveAttribute('tabindex', '0')
    expect(body).toHaveTextContent('The dossier')
  })
})

describe('X-8 — the type comes from the registry, and unknown is not NPC', () => {
  it('names a known type', () => {
    render(pane())
    expect(screen.getByText('NPC Dossier')).toBeInTheDocument()
  })

  it('renders a neutral placeholder for a type this bundle does not know', () => {
    render(pane({ documentType: 'grimoire' }))
    expect(screen.getByText('Document')).toBeInTheDocument()
    expect(screen.queryByText('NPC Dossier')).not.toBeInTheDocument()
  })
})

describe('CANVAS-27 — the edited time is formatted here, never on the wire', () => {
  it('renders a machine-readable <time> beside a human one', () => {
    const { container } = render(pane({ updatedAt: '2026-09-16T19:36:00Z', locale: 'en-GB' }))
    const time = container.querySelector('.gm-canvas__meta time')
    expect(time).toHaveAttribute('datetime', '2026-09-16T19:36:00Z')
    expect(time?.textContent).not.toBe('2026-09-16T19:36:00Z')
    expect(time?.textContent).toContain('2026')
  })

  it('shows a value it cannot format as plain text', () => {
    const { container } = render(pane({ updatedAt: 'edited 7:36 PM' }))
    expect(container.querySelector('.gm-canvas__meta time')).toBeNull()
    expect(screen.getByText('Edited edited 7:36 PM')).toBeInTheDocument()
  })
})

describe('CANVAS-13 — one aggregate status in one polite live region', () => {
  it('has exactly one live region, and it carries only the status', () => {
    render(pane({ saveStatus: 'saving' }))
    const live = screen.getAllByRole('status')
    expect(live).toHaveLength(1)
    expect(live[0]).toHaveAttribute('aria-live', 'polite')
    expect(live[0]).toHaveTextContent('Saving…')
  })

  it.each([
    ['saved', 'Saved'],
    ['saving', 'Saving…'],
    ['unsaved', 'Unsaved changes'],
    ['conflict', 'Conflict — review'],
  ] as const)('reads %s as %s', (status, message) => {
    render(pane({ saveStatus: status }))
    expect(screen.getByRole('status')).toHaveTextContent(message)
  })

  it("names Retry on a failed save and offers it as a control (STATE-2)", async () => {
    const onRetrySave = vi.fn()
    render(pane({ saveStatus: 'error', onRetrySave }))
    expect(screen.getByRole('status')).toHaveTextContent("Couldn't save — Retry")
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetrySave).toHaveBeenCalledTimes(1)
  })

  it('does not name a Retry that is not wired', () => {
    render(pane({ saveStatus: 'error' }))
    expect(screen.getByRole('status')).toHaveTextContent("Couldn't save")
    expect(screen.getByRole('status')).not.toHaveTextContent('Retry')
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('does not touch the live region when an unrelated prop re-renders the pane', () => {
    const { rerender } = render(pane({ saveStatus: 'saving', children: <p>First</p> }))
    const live = screen.getByRole('status')
    const observer = new MutationObserver(() => undefined)
    observer.observe(live, { childList: true, characterData: true, subtree: true })

    rerender(pane({ saveStatus: 'saving', title: 'Sister Ondrey Vashe', children: <p>Second</p> }))
    expect(observer.takeRecords()).toHaveLength(0)

    rerender(pane({ saveStatus: 'saved', children: <p>Second</p> }))
    expect(observer.takeRecords().length).toBeGreaterThan(0)
    observer.disconnect()
  })
})

describe('REVEAL-13 / REVEAL-14 — the header repeats the live projection', () => {
  it('offers Reveal to party and says nothing while the document is hidden', () => {
    render(pane({ onReveal: vi.fn(), onStopReveal: vi.fn() }))
    expect(screen.getByRole('button', { name: 'Reveal to party' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop showing' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Revealed/)).not.toBeInTheDocument()
  })

  it('names what is live and keeps Stop a separate control', async () => {
    const onReveal = vi.fn()
    const onStopReveal = vi.fn()
    render(
      pane({
        reveal: { state: 'revealed', summary: 'portrait, name & voice' },
        onReveal,
        onStopReveal,
      }),
    )
    expect(screen.getByText('Revealed · portrait, name & voice')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Change what the table sees' }))
    expect(onReveal).toHaveBeenCalledTimes(1)
    expect(onStopReveal).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Stop showing' }))
    expect(onStopReveal).toHaveBeenCalledTimes(1)
  })

  it('REVEAL-8: says the table is behind and offers Update…', () => {
    render(
      pane({
        reveal: { state: 'revealed', summary: 'notes', behindLatest: true },
        onReveal: vi.fn(),
        onStopReveal: vi.fn(),
      }),
    )
    expect(screen.getByText('Table is seeing an earlier version')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update…' })).toBeInTheDocument()
  })

  it('reads an unconfirmed state as reconnecting, keeps Stop and offers no widening', () => {
    render(pane({ reveal: { state: 'unknown' }, onReveal: vi.fn(), onStopReveal: vi.fn() }))
    expect(screen.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reveal to party' })).not.toBeInTheDocument()
    expect(screen.queryByText(/nothing revealed/i)).not.toBeInTheDocument()
  })

  it('renders no reveal controls when neither handler is wired', () => {
    render(pane({ reveal: { state: 'revealed', summary: 'name' } }))
    expect(screen.getByText('Revealed · name')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop showing' })).not.toBeInTheDocument()
  })
})

describe('§10.2 — the three canvas-header presentations', () => {
  it('wide shows the meta line, the version chip, export and close', () => {
    const { container } = render(
      pane({
        updatedAt: '2026-09-16T19:36:00Z',
        versionNumber: 3,
        onToggleHistory: vi.fn(),
        onExport: vi.fn(),
        onRequestClose: vi.fn(),
      }),
    )
    expect(container.querySelector('.gm-canvas__meta--offscreen')).toBeNull()
    expect(screen.getByRole('button', { name: 'v3 — version history' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close canvas' })).toBeInTheDocument()
  })

  it('compact hides the meta line visually, keeps it for assistive technology, and makes Reveal an icon', () => {
    const { container } = render(
      pane({ layout: 'compact', updatedAt: '2026-09-16T19:36:00Z', onReveal: vi.fn() }),
    )
    expect(container.querySelector('.gm-canvas__meta--offscreen')).not.toBeNull()
    expect(screen.getByText('NPC Dossier')).toBeInTheDocument()
    const reveal = screen.getByRole('button', { name: 'Reveal to party' })
    expect(reveal).toHaveClass('aether-icon-btn')
  })

  it('compact keeps Stop showing’s text', () => {
    render(
      pane({
        layout: 'compact',
        reveal: { state: 'revealed', summary: 'name' },
        onStopReveal: vi.fn(),
      }),
    )
    expect(screen.getByRole('button', { name: 'Stop showing' })).toHaveClass('aether-btn')
  })

  it('narrow replaces close with a back control and moves export and history into a menu', () => {
    render(
      pane({
        layout: 'fullScreen',
        versionNumber: 3,
        onToggleHistory: vi.fn(),
        onExport: vi.fn(),
        onRequestClose: vi.fn(),
      }),
    )
    expect(screen.queryByRole('button', { name: 'Close canvas' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'More canvas actions' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('narrow keeps reveal and Stop showing in the header', () => {
    render(
      pane({
        layout: 'fullScreen',
        reveal: { state: 'revealed', summary: 'name' },
        onReveal: vi.fn(),
        onStopReveal: vi.fn(),
      }),
    )
    expect(screen.getByRole('button', { name: 'Change what the table sees' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
  })

  it('narrow with nothing to overflow renders no menu', () => {
    render(pane({ layout: 'fullScreen', onRequestClose: vi.fn() }))
    expect(screen.queryByRole('button', { name: 'More canvas actions' })).not.toBeInTheDocument()
  })
})

describe('§10.2 — the narrow overflow menu (LAYOUT-10: never traps focus)', () => {
  function narrow(extra: Partial<CanvasPaneProps> = {}): Partial<CanvasPaneProps> {
    return { layout: 'fullScreen', versionNumber: 3, onToggleHistory: vi.fn(), onExport: vi.fn(), ...extra }
  }

  it('opens on the trigger, focuses its first item and lists both actions', async () => {
    render(pane(narrow()))
    await userEvent.click(screen.getByRole('button', { name: 'More canvas actions' }))
    const menu = screen.getByRole('menu', { name: 'Canvas actions' })
    const items = within(menu).getAllByRole('menuitem')
    expect(items).toHaveLength(2)
    expect(within(menu).getByRole('menuitem', { name: 'Version history' })).toBe(items[0])
    expect(within(menu).getByRole('menuitem', { name: 'Export' })).toBe(items[1])
    await waitFor(() => expect(items[0]).toHaveFocus())
  })

  it('moves between items with the arrow keys and wraps', async () => {
    render(pane(narrow()))
    await userEvent.click(screen.getByRole('button', { name: 'More canvas actions' }))
    const items = within(screen.getByRole('menu')).getAllByRole('menuitem')
    await waitFor(() => expect(items[0]).toHaveFocus())
    await userEvent.keyboard('{ArrowDown}')
    expect(items[1]).toHaveFocus()
    await userEvent.keyboard('{ArrowDown}')
    expect(items[0]).toHaveFocus()
    await userEvent.keyboard('{ArrowUp}')
    expect(items[1]).toHaveFocus()
  })

  it('closes on Escape and returns focus to its trigger', async () => {
    render(pane(narrow()))
    const trigger = screen.getByRole('button', { name: 'More canvas actions' })
    await userEvent.click(trigger)
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closes on a press outside itself', async () => {
    render(
      <>
        <button type="button">Elsewhere</button>
        {pane(narrow())}
      </>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'More canvas actions' }))
    await userEvent.click(screen.getByRole('button', { name: 'Elsewhere' }))
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('runs an action, closes and returns focus to the trigger', async () => {
    const onExport = vi.fn()
    const onToggleHistory = vi.fn()
    render(pane(narrow({ onExport, onToggleHistory })))
    const trigger = screen.getByRole('button', { name: 'More canvas actions' })
    await userEvent.click(trigger)
    await userEvent.click(screen.getByRole('menuitem', { name: 'Export' }))
    expect(onExport).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()

    await userEvent.click(trigger)
    await userEvent.click(screen.getByRole('menuitem', { name: 'Version history' }))
    expect(onToggleHistory).toHaveBeenCalledTimes(1)
  })

  it('leaves keys it does not handle alone', async () => {
    const onEscape = vi.fn()
    render(
      <div onKeyDown={(event) => event.key === 'Escape' && onEscape()}>
        {pane(narrow())}
      </div>,
    )
    const trigger = screen.getByRole('button', { name: 'More canvas actions' })

    // Closed: Escape belongs to whatever owns the canvas.
    trigger.focus()
    await userEvent.keyboard('{Escape}')
    expect(onEscape).toHaveBeenCalledTimes(1)

    // Open: a key the menu does not use changes nothing, and Escape is its own.
    await userEvent.click(trigger)
    await userEvent.keyboard('a')
    expect(screen.getByRole('menu')).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('closes again when the trigger is pressed a second time', async () => {
    render(pane(narrow()))
    const trigger = screen.getByRole('button', { name: 'More canvas actions' })
    await userEvent.click(trigger)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    await userEvent.click(trigger)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})

describe('CANVAS-27 — the history disclosure', () => {
  it('renders no disclosure without a version', () => {
    render(pane({ onToggleHistory: vi.fn() }))
    expect(screen.queryByRole('button', { name: /version history/i })).not.toBeInTheDocument()
  })

  it('carries aria-expanded and aria-controls, and toggles on press', async () => {
    const onToggleHistory = vi.fn()
    render(pane({ versionNumber: 3, onToggleHistory }))
    const chip = screen.getByRole('button', { name: 'v3 — version history' })
    expect(chip).toHaveAttribute('aria-expanded', 'false')
    expect(chip.getAttribute('aria-controls')).toBeTruthy()
    await userEvent.click(chip)
    expect(onToggleHistory).toHaveBeenCalledTimes(1)
  })

  it('keeps the controlled panel out of the accessibility tree while closed', () => {
    const { container } = render(pane({ versionNumber: 3, onToggleHistory: vi.fn() }))
    const chip = screen.getByRole('button', { name: 'v3 — version history' })
    const panel = container.querySelector(`#${CSS.escape(chip.getAttribute('aria-controls') ?? '')}`)
    expect(panel).not.toBeNull()
    expect(panel).toHaveAttribute('hidden')
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('shows the history when open, under its own heading, with the current version marked', () => {
    render(
      pane({
        versionNumber: 3,
        historyOpen: true,
        onToggleHistory: vi.fn(),
        history: { versions: VERSIONS, onRestore: vi.fn() },
      }),
    )
    expect(screen.getByRole('button', { name: 'v3 — version history' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('heading', { level: 3, name: 'Version history' })).toBeInTheDocument()
    const rows = screen.getAllByRole('listitem')
    expect(rows[0]).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: 'Restore v2' })).toBeInTheDocument()
  })

  it('passes the document type through, so changed fields read as the registry names them', () => {
    const { container } = render(
      pane({
        versionNumber: 3,
        historyOpen: true,
        onToggleHistory: vi.fn(),
        history: {
          versions: [{ ...VERSIONS[0], changed_fields: ['if_attacked'] }],
        },
      }),
    )
    expect(container.querySelector('.gm-versions__fields')).toHaveTextContent('Changed: If the party attacks')
  })

  it('lets the caller point the list at another current version', () => {
    render(
      pane({
        versionNumber: 3,
        historyOpen: true,
        onToggleHistory: vi.fn(),
        history: { versions: VERSIONS, currentVersionNumber: 2, documentType: 'npc', locale: 'en-GB' },
      }),
    )
    expect(screen.getAllByRole('listitem')[1]).toHaveAttribute('aria-current', 'true')
  })
})

describe('CANVAS-29 — the gold wash is acknowledged in the header', () => {
  it('offers Got it while a live change is unacknowledged', async () => {
    const onAcknowledgeChange = vi.fn()
    const { container } = render(pane({ changeHighlight: true, onAcknowledgeChange }))
    expect(container.querySelector('.gm-canvas__body')).toHaveAttribute('data-wash', 'true')
    await userEvent.click(screen.getByRole('button', { name: 'Got it' }))
    expect(onAcknowledgeChange).toHaveBeenCalledTimes(1)
  })

  it('offers nothing when no change arrived', () => {
    const { container } = render(pane({ onAcknowledgeChange: vi.fn() }))
    expect(screen.queryByRole('button', { name: 'Got it' })).not.toBeInTheDocument()
    expect(container.querySelector('.gm-canvas__body')).not.toHaveAttribute('data-wash')
  })
})

describe('CANVAS-32 — closing is guarded and focus returns', () => {
  function Harness({
    withOpener = true,
    openerRef,
    onRequestClose,
    layout,
    onBack,
  }: {
    withOpener?: boolean
    openerRef?: React.RefObject<HTMLElement | null>
    onRequestClose?: CanvasPaneProps['onRequestClose']
    layout?: CanvasPaneProps['layout']
    onBack?: () => void
  }): React.JSX.Element {
    const fallbackOpener = React.useRef<HTMLButtonElement>(null)
    const composer = React.useRef<HTMLTextAreaElement>(null)
    return (
      <>
        {withOpener && (
          <button type="button" ref={fallbackOpener}>
            Open in canvas
          </button>
        )}
        <textarea aria-label="Message the assistant" ref={composer} />
        <CanvasPane
          title="Sister Ondrey Vashe"
          documentType="npc"
          layout={layout}
          openerRef={openerRef ?? fallbackOpener}
          composerRef={composer}
          onRequestClose={onRequestClose}
          onBack={onBack}
        />
      </>
    )
  }

  it('returns focus to the control that opened the canvas', async () => {
    const onRequestClose = vi.fn()
    render(<Harness onRequestClose={onRequestClose} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close canvas' }))
    expect(onRequestClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open in canvas' })).toHaveFocus())
  })

  it('returns focus to the composer when the opener is gone', async () => {
    render(<Harness withOpener={false} onRequestClose={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus())
  })

  it('returns focus to the composer when the opener is detached from the document', async () => {
    const detached: React.RefObject<HTMLElement | null> = { current: document.createElement('button') }
    render(<Harness openerRef={detached} onRequestClose={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus())
  })

  it('leaves focus alone when the loss guard refuses the close', async () => {
    render(<Harness onRequestClose={() => false} />)
    const close = screen.getByRole('button', { name: 'Close canvas' })
    await userEvent.click(close)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(close).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Open in canvas' })).not.toHaveFocus()
  })

  it('waits for an asynchronous guard before moving focus', async () => {
    render(<Harness onRequestClose={() => Promise.resolve(true)} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open in canvas' })).toHaveFocus())
  })

  it('renders no close control when nothing handles it', () => {
    render(<Harness />)
    expect(screen.queryByRole('button', { name: 'Close canvas' })).not.toBeInTheDocument()
  })

  it('narrow: Back runs the caller’s own handler when it has one', async () => {
    const onBack = vi.fn()
    const onRequestClose = vi.fn()
    render(<Harness layout="fullScreen" onBack={onBack} onRequestClose={onRequestClose} />)
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledTimes(1)
    expect(onRequestClose).not.toHaveBeenCalled()
  })

  it('narrow: Back falls back to the guarded close, so it is never a dead control', async () => {
    const onRequestClose = vi.fn()
    render(<Harness layout="fullScreen" onRequestClose={onRequestClose} />)
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(onRequestClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open in canvas' })).toHaveFocus())
  })
})

describe('every icon in the header is either labelled or hidden', () => {
  it('marks decorative icons aria-hidden and names every control', () => {
    const { container } = render(
      pane({
        updatedAt: '2026-09-16T19:36:00Z',
        versionNumber: 3,
        onToggleHistory: vi.fn(),
        onExport: vi.fn(),
        onRequestClose: vi.fn(),
        onReveal: vi.fn(),
      }),
    )
    for (const icon of container.querySelectorAll('.material-symbols-rounded')) {
      expect(icon).toHaveAttribute('aria-hidden', 'true')
    }
    for (const button of container.querySelectorAll('button, [role="button"]')) {
      expect((button.getAttribute('aria-label') ?? button.textContent ?? '').trim().length).toBeGreaterThan(0)
    }
  })

  it('holds the 44 px touch floor on the ds controls it renders', () => {
    render(pane({ onExport: vi.fn(), onRequestClose: vi.fn(), onReveal: vi.fn() }))
    expect(screen.getByRole('button', { name: 'Export' })).toHaveAttribute('data-touch-target', 'true')
    expect(screen.getByRole('button', { name: 'Close canvas' })).toHaveAttribute('data-touch-target', 'true')
  })
})
