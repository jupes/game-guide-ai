/**
 * WorkspaceShell — the whole signed-in workspace: TopBar, AppHeader, LeftNav
 * and ChatPane assembled.
 *
 * Nothing inside takes props, so the seam is the network. One stub answers
 * every endpoint the workspace reaches for on mount (`/models`, a
 * conversation's messages, its attachments) and `/chat` when a turn is sent.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTarget, expectTouchTargets } from '../../.storybook/touchTarget'
import {
  atViewport,
  expectLeftEdge,
  expectNoPageOverflow,
  expectViewport,
  type ViewportName,
} from '../../.storybook/viewports'
import { WorkspaceShell } from './WorkspaceShell'

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'sonnet', display_name: 'Sonnet — balanced' },
  ],
}

const MESSAGES = [
  {
    id: 1,
    role: 'user',
    content: 'What does a shield spell stop?',
    mode: 'sage',
    created_at: '2026-09-18T19:02:00Z',
  },
  {
    id: 2,
    role: 'assistant',
    content: 'It stops the triggering attack, and *magic missile* outright.',
    mode: 'sage',
    created_at: '2026-09-18T19:02:04Z',
  },
]

/**
 * Answers everything the workspace asks for on mount, plus a chat turn.
 * `conversation_id` is not optional decoration: the client validates both
 * bodies with zod, and a response without it degrades to "unreadable".
 */
function workspaceApi(messages: unknown[] = MESSAGES) {
  return stubFetch((url) => {
    if (url.includes('/models')) return json(CATALOG)
    if (url.includes('/messages')) return json({ conversation_id: 'story', messages })
    if (url.includes('/attachments')) return json({ conversation_id: 'story', attachments: [] })
    if (url.includes('/chat')) {
      return json({ answer: 'A reaction, and worth the slot.', sources: [], answerable: true })
    }
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

const meta = {
  title: 'Shell/WorkspaceShell',
  component: WorkspaceShell,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: workspaceApi(),
  decorators: [
    withShell({
      conversations: [
        { mode: 'sage', firstPrompt: 'Shield spell: what does it stop?' },
        { mode: 'sage', firstPrompt: 'Grappling, briefly' },
      ],
      selected: 0,
    }),
  ],
} satisfies Meta<typeof WorkspaceShell>

export default meta
type Story = StoryObj<typeof meta>

/** The workspace as a DM opens it: a conversation selected and recalled. */
export const Playground: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText(/magic missile/)).toBeInTheDocument()
    // Brand lives once, in the TopBar (swe1.10) — never duplicated in the nav.
    await expect(canvas.getAllByText('Aetheril')).toHaveLength(1)
  },
}

/** A fresh account: no conversations, and the channel's own empty prompt. */
export const NothingYet: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('Ask the Sage…')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'New conversation' })).toBeEnabled()
  },
}

/** History recall failed for the open conversation. The workspace still works. */
export const HistoryUnavailable: Story = {
  beforeEach: stubFetch((url) => {
    if (url.includes('/models')) return json(CATALOG)
    if (url.includes('/messages')) return json({ detail: 'nope' }, 503)
    if (url.includes('/attachments')) return json({ conversation_id: 'story', attachments: [] })
    return json({ detail: 'unrouted' }, 404)
  }),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('textbox')).toBeEnabled()
  },
}

/** A player: three channels, and no GM chip anywhere in the shell. */
export const Player: Story = {
  decorators: [
    withShell({ role: 'player', conversations: [{ mode: 'sage' }], selected: 0 }),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  },
}

/**
 * Keyboard only, across the whole shell: open the second conversation from the
 * nav with Enter and watch the TopBar title follow.
 */
export const ConversationOpenedByKeyboard: Story = {
  beforeEach: workspaceApi([]),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const second = await canvas.findByRole('button', { name: 'Grappling, briefly' })
    second.focus()
    await userEvent.keyboard('{Enter}')
    await expect(second).toHaveAttribute('aria-pressed', 'true')
    await expect(
      canvasElement.querySelector('.top-bar__conversation-title'),
    ).toHaveTextContent('Grappling, briefly')
  },
}

/**
 * agent-forge-harness-27h, rework 1 — the assembled shell's tab order, written
 * down.
 *
 * ChatPane's transcript became a tab stop in this branch: it is the scroller,
 * and `scrollable-region-focusable` (WCAG 2.1.1) wants a keyboard user to be
 * able to scroll back through their own conversation. That is a NEW stop on
 * every keyboard user's way to the composer, and it is UNCONDITIONAL — an
 * empty or two-line thread gets it too, where there is nothing to scroll, so
 * it is a dead stop there. That is a deliberate trade (measuring overflow to
 * decide would mean a ResizeObserver and a re-render on every message, for a
 * stop that is correct whenever it matters), but until now nothing in the
 * repository measured the shell's tab order at all, so the stop existed in no
 * test and any later change to it would have been invisible.
 *
 * This walks the whole shell with real Tab presses and records what it finds.
 */
export const ShellTabOrder: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await canvas.findByText(/magic missile/)

    const transcript = canvas.getByRole('region', { name: 'Conversation' })
    const composer = canvas.getByRole('textbox')

    // Walk until focus leaves the shell or wraps round to something already
    // seen — a bounded loop, because Tab cycles within the document.
    const order: Element[] = []
    for (let i = 0; i < 40; i += 1) {
      await userEvent.tab()
      const el = document.activeElement
      if (!el || !canvasElement.contains(el) || order.includes(el)) break
      order.push(el)
    }

    await expect(order).toContain(transcript)
    await expect(order).toContain(composer)
    // The transcript comes first: you tab past the conversation into the box
    // you answer it in, not the other way round.
    await expect(order.indexOf(transcript)).toBeLessThan(order.indexOf(composer))
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkNothingYet: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell()],
}

// ── Narrow layout (agent-forge-harness-0rn, LAYOUT-3) ─────────────────────────
// Below 768px there is no sidebar: TopBar's "Open navigation" opens LeftNav as
// a modal drawer, and ModelPicker and the theme control live in it. Every
// story that sets a viewport starts with the canary (expectViewport), so a
// silently skipped resize fails instead of running the story at 1200px.

const FIRST = 'Shield spell: what does it stop?'
const SECOND = 'Grappling, briefly'
const CHANNELS = ['Sage', 'Spell', 'Rules', 'GM']

async function expectClosedPhone(canvasElement: HTMLElement, viewport: ViewportName): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await canvas.findByText(/magic missile/)
  await expectNoPageOverflow()
  const menu = canvas.getByRole('button', { name: 'Open navigation' })
  await expectTouchTarget(canvas, 'Open navigation')
  await expect(menu.getBoundingClientRect().left).toBeLessThan(60)
  // LeftNav is mounted (the host is always rendered) but not rendered: its
  // controls are out of the accessibility tree until the drawer opens.
  await expect(canvasElement.querySelector('.left-nav')).not.toBeNull()
  await expect(canvasElement.querySelector('.left-nav')).not.toBeVisible()
  await expect(canvas.queryByRole('button', { name: 'New conversation' })).toBeNull()
  await expect(canvas.getByRole('navigation', { name: 'Channels' })).toBeVisible()
  await expectTouchTargets(canvas, CHANNELS)
  const send = canvas.getByRole('button', { name: 'Send message' })
  await expect(send.getBoundingClientRect().bottom).toBeLessThanOrEqual(window.innerHeight)
}

export const PhoneClosed390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => expectClosedPhone(canvasElement, 'phone390'),
}

export const PhoneClosed375: Story = {
  ...atViewport('phone375'),
  play: async ({ canvasElement }) => expectClosedPhone(canvasElement, 'phone375'),
}

export const PhoneClosed320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => {
    await expectClosedPhone(canvasElement, 'phone320')
    // The chat sits on the phone gutter, not the desktop's 24px padding.
    const pane = canvasElement.querySelector('.chat-pane')
    if (!(pane instanceof HTMLElement)) throw new Error('no chat pane')
    await expectLeftEdge(pane, 16)
  },
}

export const DarkPhoneClosed390: Story = {
  ...atViewport('phone390', 'dark'),
  play: async ({ canvasElement }) => expectClosedPhone(canvasElement, 'phone390'),
}

async function openDrawer(canvasElement: HTMLElement): Promise<HTMLElement> {
  const canvas = within(canvasElement)
  await canvas.findByText(/magic missile/)
  await userEvent.click(canvas.getByRole('button', { name: 'Open navigation' }))
  return canvas.getByRole('dialog', { name: 'Navigation' })
}

/** The drawer is on top, the scrim is between it and the page, and at least
 * 48px of scrim stays tappable on the right. */
async function expectDrawerOverScrim(drawer: HTMLElement): Promise<void> {
  const box = drawer.getBoundingClientRect()
  await expect(box.left).toBe(0)
  await expect(box.right).toBeLessThanOrEqual(window.innerWidth - 48)
  const atCentre = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2)
  await expect(atCentre !== null && drawer.contains(atCentre)).toBe(true)
  const atEdge = document.elementFromPoint(window.innerWidth - 12, window.innerHeight / 2)
  await expect(atEdge).toHaveClass('workspace-shell__scrim')
}

/**
 * The modal promise in a real browser. user-event's tab() ignores `inert` and
 * then calls .focus(), which Chromium refuses on an inert element — so a
 * missing wrap would leave focus stuck on the last stop, still "inside". Each
 * step must therefore MOVE, and the walk must come round to the first stop.
 */
async function expectTabWalkStaysInside(drawer: HTMLElement): Promise<void> {
  const close = within(drawer).getByRole('button', { name: 'Close navigation' })
  let previous = document.activeElement
  let closeVisits = 0
  for (let i = 0; i < 25; i += 1) {
    await userEvent.tab()
    const active = document.activeElement
    await expect(active !== null && drawer.contains(active)).toBe(true)
    await expect(active).not.toBe(previous)
    if (active === close) closeVisits += 1
    previous = active
  }
  await expect(closeVisits).toBeGreaterThanOrEqual(2)
}

async function expectOpenDrawer(canvasElement: HTMLElement, viewport: ViewportName): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  const drawer = await openDrawer(canvasElement)
  await expectDrawerOverScrim(drawer)
  // Every query about the open drawer is scoped to it: dom-testing-library
  // does not treat `inert` as hidden, so the header's chips would match too.
  const inDrawer = within(drawer)
  await expectTouchTargets(inDrawer, [
    'Close navigation',
    ...CHANNELS,
    'New conversation',
    FIRST,
    SECOND,
    `Rename ${FIRST}`,
    `Rename ${SECOND}`,
    'Open user menu',
  ])
  await expectTouchTarget(inDrawer, 'Model', 'combobox')
  await expectTouchTarget(inDrawer, 'Dark theme', 'switch')
  await expectTabWalkStaysInside(drawer)
  // Programmatic focus on purpose: the claim is that the inert chat REFUSES
  // focus, which only a real browser enforces.
  const composer = canvas.getByPlaceholderText('Ask…')
  composer.focus()
  await expect(document.activeElement).not.toBe(composer)
}

export const PhoneDrawerOpens320: Story = {
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => expectOpenDrawer(canvasElement, 'phone320'),
}

export const DarkPhoneDrawerOpens390: Story = {
  ...atViewport('phone390', 'dark'),
  play: async ({ canvasElement }) => expectOpenDrawer(canvasElement, 'phone390'),
}

/** A folding phone's 280px cover screen: the only width where the drawer's
 * `max-width` binds, keeping 48px of scrim to tap. */
export const PhoneDrawerOpens280: Story = {
  ...atViewport('fold280'),
  play: async ({ canvasElement }) => {
    await expectViewport('fold280')
    const drawer = await openDrawer(canvasElement)
    await expect(drawer.getBoundingClientRect().right).toBeLessThanOrEqual(232)
    await expectDrawerOverScrim(drawer)
  },
}

/** A phone in landscape is narrow but short: the drawer scrolls as a whole,
 * so the account footer and the last conversation are still reachable. */
export const PhoneLandscapeDrawer667: Story = {
  ...atViewport('landscape667'),
  play: async ({ canvasElement }) => {
    await expectViewport('landscape667')
    const drawer = await openDrawer(canvasElement)
    await expect(drawer.scrollHeight).toBeGreaterThan(drawer.clientHeight)
    for (const name of [SECOND, 'Open user menu']) {
      const control = within(drawer).getByRole('button', { name })
      control.scrollIntoView({ block: 'nearest' })
      const box = control.getBoundingClientRect()
      await expect(box.top).toBeGreaterThanOrEqual(0)
      await expect(box.bottom).toBeLessThanOrEqual(window.innerHeight)
      const hit = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2)
      await expect(hit !== null && control.contains(hit)).toBe(true)
    }
  },
}

/** Picking a conversation is navigation: the drawer closes on it, the title
 * follows, and focus returns to the menu button. */
export const PhoneDrawerPicksConversation390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => {
    await expectViewport('phone390')
    const canvas = within(canvasElement)
    const drawer = await openDrawer(canvasElement)
    await userEvent.click(within(drawer).getByRole('button', { name: SECOND }))
    await expect(canvas.queryByRole('dialog')).toBeNull()
    await expect(canvasElement.querySelector('.top-bar__conversation-title')).toHaveTextContent(SECOND)
    await expect(canvas.getByRole('button', { name: 'Open navigation' })).toHaveFocus()
  },
}

/** Closed, the drawer contributes no tab stops: the walk starts at the menu
 * button and goes through the transcript to the composer. */
export const PhoneTabOrder390: Story = {
  ...atViewport('phone390'),
  play: async ({ canvasElement }) => {
    await expectViewport('phone390')
    const canvas = within(canvasElement)
    await canvas.findByText(/magic missile/)
    const transcript = canvas.getByRole('region', { name: 'Conversation' })
    const composer = canvas.getByPlaceholderText('Ask…')

    const order: Element[] = []
    for (let i = 0; i < 40; i += 1) {
      await userEvent.tab()
      const el = document.activeElement
      if (!el || !canvasElement.contains(el) || order.includes(el)) break
      order.push(el)
    }

    await expect(order[0]).toBe(canvas.getByRole('button', { name: 'Open navigation' }))
    await expect(order).toContain(transcript)
    await expect(order).toContain(composer)
    await expect(order.indexOf(transcript)).toBeLessThan(order.indexOf(composer))
    await expect(order.filter((el) => el.closest('.left-nav') !== null)).toHaveLength(0)
  },
}

/** One pixel below LAYOUT-3's boundary: still the narrow layout. */
export const Narrow767: Story = {
  ...atViewport('narrow767'),
  play: async ({ canvasElement }) => {
    await expectViewport('narrow767')
    const canvas = within(canvasElement)
    await canvas.findByText(/magic missile/)
    await expect(canvas.getByRole('button', { name: 'Open navigation' })).toBeVisible()
    await expect(canvasElement.querySelector('.left-nav')).not.toBeVisible()
  },
}

/** At 768px the sidebar is back, at its 268px, and the picker is in the header. */
export const Wide768: Story = {
  ...atViewport('wide768'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide768')
    const canvas = within(canvasElement)
    await canvas.findByText(/magic missile/)
    await expect(canvas.queryByRole('button', { name: 'Open navigation' })).toBeNull()
    const nav = canvas.getByRole('navigation', { name: 'Main navigation' }).getBoundingClientRect()
    await expect(nav.left).toBe(0)
    await expect(nav.width).toBe(268)
    const channels = canvas.getByRole('navigation', { name: 'Channels' })
    await expect(within(channels).getByRole('combobox', { name: 'Model' })).toBeVisible()
  },
}

/** The desktop workspace, measured: nothing about it moved. */
export const Wide1280Unchanged: Story = {
  ...atViewport('wide1280'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide1280')
    const canvas = within(canvasElement)
    await canvas.findByText(/magic missile/)
    const box = (selector: string): DOMRect => {
      const el = canvasElement.querySelector(selector)
      if (el === null) throw new Error(`no ${selector}`)
      return el.getBoundingClientRect()
    }
    await expect(box('.top-bar').height).toBe(64)
    await expect(box('.app-header').height).toBeGreaterThanOrEqual(52)
    await expect(box('.left-nav').left).toBe(0)
    await expect(box('.left-nav').width).toBe(268)
    await expect(box('main').left).toBe(268)
    await expect(canvas.queryByRole('dialog')).toBeNull()
    await expect(canvasElement.querySelectorAll('[inert]')).toHaveLength(0)
  },
}
