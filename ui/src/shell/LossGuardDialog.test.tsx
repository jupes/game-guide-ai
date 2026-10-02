/**
 * LossGuardDialog.test.tsx -- the unsaved-changes dialog (agent-forge-harness-1kg.6.3,
 * T-7; CANVAS-16, §5.3, AE-47, LAYOUT-10).
 *
 * The component is props-driven: the provider decides when it opens and what each
 * button resolves (`canvasContext.test.tsx`). What this file proves is the dialog's
 * own contract: its name, its default focus, its keyboard, and where focus goes next.
 */

import * as React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LossGuardDialog, type LossGuardDialogProps } from './LossGuardDialog'

function props(over: Partial<LossGuardDialogProps> = {}): LossGuardDialogProps {
  return {
    title: 'Sister Ondrey Vashe',
    retryable: true,
    busy: false,
    onKeep: vi.fn(),
    onRetry: vi.fn(),
    onDiscard: vi.fn(),
    ...over,
  }
}

function Harness(extra: Partial<LossGuardDialogProps> & { open?: boolean }): React.JSX.Element {
  const { open = true, ...rest } = extra
  return (
    <div>
      <button type="button">Opener</button>
      {open && <LossGuardDialog {...props(rest)} />}
      <button type="button">Behind</button>
    </div>
  )
}

describe('LossGuardDialog', () => {
  it('is a modal dialog named by its heading, which carries the document title (§5.3)', () => {
    render(<LossGuardDialog {...props()} />)
    const dialog = screen.getByRole('dialog', { name: 'You have unsaved changes to Sister Ondrey Vashe' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('You have unsaved changes to Sister Ondrey Vashe')
  })

  it('focuses Keep editing, not the destructive button, when it opens (AE-47)', () => {
    render(<LossGuardDialog {...props()} />)
    expect(screen.getByRole('button', { name: 'Keep editing' })).toHaveFocus()
  })

  it('offers Try saving again only when a retry can help', () => {
    const { rerender } = render(<LossGuardDialog {...props({ retryable: true })} />)
    expect(screen.getByRole('button', { name: 'Try saving again' })).toBeEnabled()
    rerender(<LossGuardDialog {...props({ retryable: false })} />)
    expect(screen.queryByRole('button', { name: 'Try saving again' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Keep editing' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeVisible()
  })

  it('routes each button to its own handler, and only Discard changes discards', async () => {
    const user = userEvent.setup()
    const p = props()
    render(<LossGuardDialog {...p} />)
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect([p.onKeep, p.onRetry, p.onDiscard].map((fn) => vi.mocked(fn).mock.calls.length)).toEqual([1, 0, 0])
    await user.click(screen.getByRole('button', { name: 'Try saving again' }))
    expect([p.onKeep, p.onRetry, p.onDiscard].map((fn) => vi.mocked(fn).mock.calls.length)).toEqual([1, 1, 0])
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect([p.onKeep, p.onRetry, p.onDiscard].map((fn) => vi.mocked(fn).mock.calls.length)).toEqual([1, 1, 1])
  })

  it('Escape keeps editing, and never discards', async () => {
    const user = userEvent.setup()
    const p = props()
    render(<LossGuardDialog {...p} />)
    await user.keyboard('{Escape}')
    expect(p.onKeep).toHaveBeenCalledTimes(1)
    expect(p.onDiscard).not.toHaveBeenCalled()
    expect(p.onRetry).not.toHaveBeenCalled()
  })

  it('Escape is consumed, so the nav drawer behind it does not also close', async () => {
    const user = userEvent.setup()
    const seen: boolean[] = []
    const onDocumentKey = (event: KeyboardEvent): void => {
      seen.push(event.defaultPrevented)
    }
    document.addEventListener('keydown', onDocumentKey)
    render(<LossGuardDialog {...props()} />)
    await user.keyboard('{Escape}')
    document.removeEventListener('keydown', onDocumentKey)
    expect(seen).toEqual([true])
  })

  it('Tab wraps from the last control to the first, and Shift+Tab the other way', async () => {
    const user = userEvent.setup()
    render(<LossGuardDialog {...props()} />)
    const keep = screen.getByRole('button', { name: 'Keep editing' })
    const discard = screen.getByRole('button', { name: 'Discard changes' })
    expect(keep).toHaveFocus()
    await user.tab({ shift: true })
    expect(discard).toHaveFocus()
    await user.tab()
    expect(keep).toHaveFocus()
    await user.tab()
    await user.tab()
    expect(discard).toHaveFocus()
    await user.tab()
    expect(keep).toHaveFocus()
  })

  it('without a retry the trap still holds between its two buttons', async () => {
    const user = userEvent.setup()
    render(<Harness retryable={false} />)
    const keep = screen.getByRole('button', { name: 'Keep editing' })
    const discard = screen.getByRole('button', { name: 'Discard changes' })
    await user.tab()
    expect(discard).toHaveFocus()
    await user.tab()
    expect(keep).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Behind', hidden: true })).not.toHaveFocus()
    expect(screen.getByRole('button', { name: 'Opener', hidden: true })).not.toHaveFocus()
  })

  it('returns focus to the element that had it before the dialog opened', () => {
    const { rerender } = render(<Harness open={false} />)
    const opener = screen.getByRole('button', { name: 'Opener' })
    opener.focus()
    rerender(<Harness open />)
    expect(screen.getByRole('button', { name: 'Keep editing' })).toHaveFocus()
    rerender(<Harness open={false} />)
    expect(opener).toHaveFocus()
  })

  it('while a retry runs, saying so, it cannot be pressed again and cannot discard', () => {
    render(<LossGuardDialog {...props({ busy: true })} />)
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Keep editing' })).toBeEnabled()
  })
})
