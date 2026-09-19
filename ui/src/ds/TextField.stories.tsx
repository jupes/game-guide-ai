import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { TextField } from './TextField'

const meta = {
  title: 'Aetheril/TextField',
  component: TextField,
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['filled', 'outlined'],
    },
  },
  args: { onChange: fn() },
} satisfies Meta<typeof TextField>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    label: 'Character name',
    placeholder: 'Astra Vail',
    variant: 'outlined',
  },
}

export const Variants: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
      <TextField variant="filled" label="Filled" placeholder="Ask…" />
      <TextField variant="outlined" label="Outlined" placeholder="Ask…" />
    </div>
  ),
}

export const WithIconsAndSupport: Story = {
  args: {
    label: 'Search the tomes',
    leadingIcon: 'search',
    trailingIcon: 'mic',
    supportingText: 'Searches all indexed rulebooks',
  },
}

export const ErrorState: Story = {
  args: {
    label: 'Spell slot level',
    value: 'ten',
    error: true,
    supportingText: 'Must be a number from 1 to 9',
  },
}

export const Multiline: Story = {
  args: {
    multiline: true,
    rows: 3,
    placeholder: 'Ask…',
    fullWidth: true,
  },
  parameters: { layout: 'padded' },
}

export const Disabled: Story = {
  args: {
    label: 'Sealed scroll',
    disabled: true,
  },
}

/**
 * DS extension (record SLASH-12): the control forwards a ref and the ARIA a
 * composer needs to drive a listbox — `aria-autocomplete`, `aria-controls` and
 * `aria-activedescendant` belong on the textarea, not on a wrapper.
 */
export const RefAndAriaForwarding: Story = {
  render: () => {
    function Harness() {
      const ref = React.useRef<HTMLInputElement | HTMLTextAreaElement>(null)
      const [value, setValue] = React.useState('')
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 320 }}>
          <TextField
            multiline
            autoGrow
            rows={1}
            fullWidth
            ref={ref}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label="Message"
            aria-autocomplete="list"
            aria-controls="tools-listbox"
            aria-activedescendant="tools-option-0"
          />
          <ul id="tools-listbox" role="listbox" aria-label="Tools" style={{ margin: 0, paddingLeft: 18 }}>
            <li id="tools-option-0" role="option" aria-selected>
              /npc
            </li>
          </ul>
          <button type="button" onClick={() => ref.current?.focus()}>
            Focus the composer
          </button>
        </div>
      )
    }
    return <Harness />
  },
  parameters: { layout: 'padded' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Message' })
    await expect(field).toHaveAttribute('aria-controls', 'tools-listbox')
    await expect(field).toHaveAttribute('aria-activedescendant', 'tools-option-0')
    await userEvent.click(canvas.getByRole('button', { name: /focus the composer/i }))
    await expect(field).toHaveFocus()
  },
}
