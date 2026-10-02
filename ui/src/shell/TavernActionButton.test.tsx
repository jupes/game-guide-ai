/**
 * TavernActionButton.test.tsx -- the DS button markup with ARIA passthrough
 * (agent-forge-harness-30c, PR-1).
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TavernActionButton } from './TavernActionButton'

describe('TavernActionButton', () => {
  it('is the DS button: class, variant, size, touch target, state layer and hidden icons', () => {
    render(
      <TavernActionButton variant="outlined" icon="lock" trailingIcon="play_arrow" onPress={() => {}}>
        Start
      </TavernActionButton>,
    )
    const button = screen.getByRole('button', { name: 'Start' })
    expect(button).toHaveClass('aether-btn')
    expect(button).toHaveAttribute('data-variant', 'outlined')
    expect(button).toHaveAttribute('data-size', 'medium')
    expect(button).toHaveAttribute('data-touch-target', 'true')
    expect(button).toHaveAttribute('type', 'button')
    expect(button.querySelector('.aether-btn__state')).toHaveAttribute('aria-hidden', 'true')
    const icons = Array.from(button.querySelectorAll('.material-symbols-rounded'))
    expect(icons.map((i) => i.textContent)).toEqual(['lock', 'play_arrow'])
    for (const icon of icons) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it('a press calls onPress; a locked one stays focusable, described, and calls nothing', async () => {
    const onPress = vi.fn()
    const { rerender } = render(<TavernActionButton variant="text" onPress={onPress}>Go</TavernActionButton>)
    await userEvent.click(screen.getByRole('button', { name: 'Go' }))
    expect(onPress).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Go' })).not.toHaveAttribute('aria-disabled')

    rerender(
      <>
        <TavernActionButton variant="text" ariaDisabled ariaDescribedBy="why" onPress={onPress}>Go</TavernActionButton>
        <span id="why">Because</span>
      </>,
    )
    const locked = screen.getByRole('button', { name: 'Go' })
    expect(locked).toHaveAttribute('aria-disabled', 'true')
    expect(locked).not.toBeDisabled()
    expect(locked).toHaveAccessibleDescription('Because')
    await userEvent.click(locked)
    locked.focus()
    await userEvent.keyboard('{Enter}')
    expect(locked).toHaveFocus()
    expect(onPress).toHaveBeenCalledTimes(1)
  })

  it('passes aria-expanded and aria-controls through, in both states', () => {
    const { rerender } = render(
      <TavernActionButton variant="text" ariaExpanded={false} ariaControls="region" onPress={() => {}}>Toggle</TavernActionButton>,
    )
    expect(screen.getByRole('button', { name: 'Toggle' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: 'Toggle' })).toHaveAttribute('aria-controls', 'region')
    rerender(<TavernActionButton variant="text" ariaExpanded ariaControls="region" onPress={() => {}}>Toggle</TavernActionButton>)
    expect(screen.getByRole('button', { name: 'Toggle' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('without ARIA props it adds none', () => {
    render(<TavernActionButton variant="filled" onPress={() => {}}>Plain</TavernActionButton>)
    const button = screen.getByRole('button', { name: 'Plain' })
    for (const name of ['aria-disabled', 'aria-expanded', 'aria-controls', 'aria-describedby']) {
      expect(button).not.toHaveAttribute(name)
    }
  })
})
