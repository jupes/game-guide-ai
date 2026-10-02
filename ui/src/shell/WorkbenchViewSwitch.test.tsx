/**
 * WorkbenchViewSwitch.test.tsx -- the Chat / Canvas switch (agent-forge-harness-1kg.6.3,
 * T-12; LAYOUT-5, I-4, C-10).
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkbenchViewSwitch } from './WorkbenchViewSwitch'

describe('WorkbenchViewSwitch', () => {
  it('is a group named Workbench view with two toggle buttons', () => {
    render(<WorkbenchViewSwitch view="chat" onChange={() => {}} />)
    const group = screen.getByRole('group', { name: 'Workbench view' })
    expect(group).toBeInTheDocument()
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Chat', 'Canvas'])
  })

  it.each([
    ['chat', 'true', 'false'],
    ['canvas', 'false', 'true'],
  ] as const)('with the %s view showing, Chat is %s-pressed and Canvas %s-pressed', (view, chat, canvas) => {
    render(<WorkbenchViewSwitch view={view} onChange={() => {}} />)
    expect(screen.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', chat)
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', canvas)
  })

  it('pressing a segment asks for that view, and pressing the current one asks for it again harmlessly', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<WorkbenchViewSwitch view="chat" onChange={onChange} />)
    await user.click(screen.getByRole('button', { name: 'Canvas' }))
    expect(onChange).toHaveBeenLastCalledWith('canvas')
    await user.click(screen.getByRole('button', { name: 'Chat' }))
    expect(onChange).toHaveBeenLastCalledWith('chat')
  })

  it('keeps focus on the segment that was pressed: switching views moves focus nowhere', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<WorkbenchViewSwitch view="chat" onChange={() => {}} />)
    await user.click(screen.getByRole('button', { name: 'Canvas' }))
    rerender(<WorkbenchViewSwitch view="canvas" onChange={() => {}} />)
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveFocus()
  })

  it('is operable from the keyboard', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<WorkbenchViewSwitch view="chat" onChange={onChange} />)
    await user.tab()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Canvas' })).toHaveFocus()
    await user.keyboard(' ')
    expect(onChange).toHaveBeenCalledWith('canvas')
  })
})
