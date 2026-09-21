/**
 * SelectionBar (agent-forge-harness-1kg.6.2) — CANVAS-23's three actions.
 *
 * The bar reports and does nothing else: an action carries the field key and
 * the span the GM selected, and the owner decides whether to spend money on it
 * (X-1). These tests hold it to that, and to being reachable by keyboard.
 *
 * Where the bar is allowed to appear at all — inside one editable prose field,
 * never across two — is `GameDocument`'s decision and is tested there.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { EDIT_ACTIONS } from './contracts'
import { SelectionBar, type DocumentSelection } from './SelectionBar'

const SELECTION: DocumentSelection = {
  field: 'wants',
  label: 'Wants',
  start: 4,
  end: 9,
  text: 'wants',
}

function show(overrides: Partial<React.ComponentProps<typeof SelectionBar>> = {}) {
  const onAction = vi.fn()
  const onDismiss = vi.fn()
  render(
    <SelectionBar
      selection={SELECTION}
      placement={{ left: 24, top: 96, placement: 'above' }}
      onAction={onAction}
      onDismiss={onDismiss}
      {...overrides}
    />,
  )
  return { onAction, onDismiss }
}

describe('the three actions of CANVAS-23', () => {
  it('offers exactly the actions the contract knows', () => {
    show()
    const bar = screen.getByRole('toolbar', { name: 'Ask the assistant about the selected text in Wants' })
    expect(bar).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rewrite' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Shorter' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Darker' })).toBeInTheDocument()
    expect(EDIT_ACTIONS).toEqual(['rewrite', 'shorter', 'darker'])
  })

  it.each([
    ['Rewrite', 'rewrite'],
    ['Shorter', 'shorter'],
    ['Darker', 'darker'],
  ])('%s reports the action with the field and the span', async (name, action) => {
    const user = userEvent.setup()
    const { onAction } = show()
    await user.click(screen.getByRole('button', { name }))
    expect(onAction).toHaveBeenCalledWith(action, SELECTION)
  })

  it('runs on click and calls nothing itself (X-1)', async () => {
    const user = userEvent.setup()
    const { onAction, onDismiss } = show()
    await user.click(screen.getByRole('button', { name: 'Rewrite' }))
    expect(onAction).toHaveBeenCalledTimes(1)
    expect(onDismiss).not.toHaveBeenCalled()
  })
})

describe('the keyboard', () => {
  it('reaches every action by Tab, in order', async () => {
    const user = userEvent.setup()
    show()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Rewrite' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Shorter' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Darker' })).toHaveFocus()
  })

  it('dismisses on Escape from anywhere inside the bar', async () => {
    const user = userEvent.setup()
    const { onDismiss } = show()
    await user.tab()
    await user.keyboard('{Escape}')
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})

describe('where it sits', () => {
  it('takes its position from the placement it is given, in the pane`s own coordinates', () => {
    show({ placement: { left: 24, top: 96, placement: 'above' } })
    const bar = screen.getByRole('toolbar', { name: /selected text/ })
    expect(bar).toHaveStyle({ left: '24px', top: '96px' })
    expect(bar).toHaveAttribute('data-placement', 'above')
  })

  it('says when it had to drop below the selection', () => {
    show({ placement: { left: 0, top: 30, placement: 'below' } })
    expect(screen.getByRole('toolbar', { name: /selected text/ })).toHaveAttribute('data-placement', 'below')
  })
})
