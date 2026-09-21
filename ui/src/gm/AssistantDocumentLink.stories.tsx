/**
 * AssistantDocumentLink — CANVAS-3's row, on its own.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import type { DocumentLink } from './contracts'
import { AssistantDocumentLink } from './AssistantDocumentLink'
import { DOCUMENT_LINK } from './laneFixtures'

const meta = {
  title: 'Aetheril/AssistantDocumentLink',
  component: AssistantDocumentLink,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    link: DOCUMENT_LINK as unknown as DocumentLink,
    onOpen: fn(),
  },
} satisfies Meta<typeof AssistantDocumentLink>

export default meta
type Story = StoryObj<typeof meta>

export const Dossier: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('NPC Dossier · saved to NPCs')).toBeVisible()
    // Nothing opens by itself.
    await expect(args.onOpen).not.toHaveBeenCalled()

    await userEvent.click(canvas.getByRole('button', { name: /Open in canvas/ }))
    await expect(args.onOpen).toHaveBeenCalledTimes(1)
  },
}

export const SessionNotes: Story = {
  args: {
    link: {
      document_id: 'doc_9',
      type: 'session-notes',
      title: 'Session 12 — the ledger, the ferry, the fire',
      library_category: 'session-log',
    } as unknown as DocumentLink,
  },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('Session Notes · saved to Session log')).toBeVisible()
  },
}

export const KeyboardReachable: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: /Open in canvas/ }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(args.onOpen).toHaveBeenCalledTimes(1)
  },
}

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1. This file had NO dark story, so strict axe
// had never rendered AssistantDocumentLink against the dark palette at all. That is exactly
// the hole that hid `--aether-nat20` at 4.49:1 on its dark container until a
// dark DiceRoll story was written for it: in a themed design system every
// colour is a DIFFERENT value per theme, so a light-only story is half a test.

export const Dark: Story = { ...Dossier, globals: { theme: 'dark' } }
