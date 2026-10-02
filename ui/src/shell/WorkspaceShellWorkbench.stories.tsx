/**
 * WorkspaceShell with the Workbench (agent-forge-harness-1kg.6.3) -- every layout, in
 * a real browser, in both themes.
 *
 * jsdom has no layout, so this file is where the boxes are measured: the 56px rail,
 * the 472px chat beside the canvas, the column that is concealed or hidden in a single
 * column, the composer staying inside the viewport over a long transcript (C-1), the
 * chat's one status node staying exposed while the canvas shows (C-2), the canvas
 * running edge to edge on a phone (C-15), and a phone in landscape (C-15).
 *
 * The REAL campaign provider opens the document from a deep link (`restore`); only the
 * network is stubbed. Every story passes axe (`test: 'error'`).
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, waitFor, within } from 'storybook/test'

import { tabTo } from '../../.storybook/keyboard'
import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import {
  atViewport,
  expectNoPageOverflow,
  expectNothingClipped,
  expectTheme,
  expectViewport,
  type ViewportName,
} from '../../.storybook/viewports'
import { DOCUMENT_FIXTURES } from '../gm/documentFixtures'
import { CampaignProvider } from './campaignContext'
import { WorkspaceShell } from './WorkspaceShell'

const CAMPAIGN_ID = 'cmp_StoryCampaign0000000001'
const DOC_ID = 'doc_StoryOndrey000000000001'
const THREAD_ID = 'cnv_StoryThread0000000001'
const TITLE = 'Sister Ondrey Vashe'

const CATALOG = { default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] }

const CAMPAIGN = {
  schema_version: 1, campaign_id: CAMPAIGN_ID, name: 'The Drowned Crown', created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
}

const THREAD = {
  schema_version: 1, conversation_id: THREAD_ID, campaign_id: CAMPAIGN_ID, title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

/** No portrait: the asset route is not stubbed, and an image that cannot load is noise, not a finding. */
const NPC_FIELDS = Object.fromEntries(
  Object.entries(DOCUMENT_FIXTURES.npc.data as Record<string, unknown>).filter(([key]) => key !== 'portrait'),
)
const DOCUMENT = { ...DOCUMENT_FIXTURES.npc, document_id: DOC_ID, campaign_id: CAMPAIGN_ID, data: NPC_FIELDS }

const HISTORY = {
  schema_version: 1, document_id: DOC_ID, next_cursor: null,
  items: [
    {
      number: 3, author: 'assistant', summary: 'Wants the signet', created_at: '2026-09-16T19:36:00Z', sealed: true,
      changed_fields: ['wants'], restored_from: null,
    },
  ],
}

/** `count` settled GM turns as the wire carries them, newest first, so the transcript is long enough to scroll. */
function timelinePage(count: number) {
  const items = Array.from({ length: count }, (_, at) => ({
    schema_version: 1,
    entry_kind: 'chat',
    entry_id: `ent_story${String(count - at).padStart(4, '0')}`,
    created_at: '2026-09-16T19:24:40Z',
    mode: 'gm',
    prompt: `Question number ${count - at} about the harbour`,
    answer: {
      created_at: '2026-09-16T19:24:52Z',
      text: `Answer number ${count - at}, kept short.`,
      answerable: true,
      sources: [],
    },
  }))
  return { schema_version: 1, conversation_id: THREAD_ID, items, next_cursor: null }
}

/** A tool turn whose result is a document link: the control that opens a document from the chat. */
const LINK_TITLE = 'Ondrey the Ferryman'
const TOOL_ENTRY = {
  schema_version: 1,
  entry_kind: 'tool',
  entry_id: 'ent_story_tool_0001',
  created_at: '2026-09-16T19:35:00Z',
  brief: 'a ferryman',
  invocation: {
    schema_version: 1,
    invocation_id: 'inv_0a1b2c3d4e5f6a7b',
    tool_id: 'npc',
    status: 'done',
    attempt: 1,
    cancel_requested: false,
    created_at: '2026-09-16T19:35:00Z',
    updated_at: '2026-09-16T19:35:20Z',
    result: {
      result_kind: 'document',
      tool_id: 'npc',
      prose: 'A ferryman who remembers every debt.',
      suggestions: [],
      document: { document_id: DOC_ID, type: 'npc', title: LINK_TITLE, library_category: 'npcs' },
    },
    error: null,
  },
}

interface ApiOptions {
  /** Settled turns in the thread. */
  turns?: number
  /** How long `/chat` takes to answer. */
  chatMs?: number
  /** The thread ends with a tool turn whose result is a document link. */
  toolLink?: boolean
}

function workbenchApi({ turns = 40, chatMs = 0, toolLink = false }: ApiOptions = {}) {
  return stubFetch((url, init) => {
    if (url === `/campaigns/${CAMPAIGN_ID}/library`) {
      const { category } = JSON.parse(String(init?.body ?? '{}')) as { category: string }
      const items =
        category === 'npcs'
          ? [{
              document_id: DOC_ID, type: 'npc', title: TITLE, qualifier: '', tags: [], archived: false,
              updated_at: '2026-09-16T19:36:00Z',
            }]
          : []
      return json({ schema_version: 1, campaign_id: CAMPAIGN_ID, category, items, next_cursor: null })
    }
    if (url.includes('/models')) return json(CATALOG)
    if (url === `/campaigns/${CAMPAIGN_ID}`) return json(CAMPAIGN)
    if (url === `/campaigns/${CAMPAIGN_ID}/documents/${DOC_ID}`) return json(DOCUMENT)
    if (url.startsWith(`/campaigns/${CAMPAIGN_ID}/documents/${DOC_ID}/versions`)) return json(HISTORY)
    if (url === `/conversations/${THREAD_ID}`) return json(THREAD)
    if (url.startsWith(`/conversations/${THREAD_ID}/timeline`)) {
      const page = timelinePage(turns)
      return json(toolLink ? { ...page, items: [TOOL_ENTRY, ...page.items] } : page)
    }
    if (url.includes('/attachments')) return json({ conversation_id: THREAD_ID, attachments: [] })
    if (url.includes('/chat')) {
      const answer = json({ answer: 'A settled answer.', sources: [], answerable: true, conversation_id: THREAD_ID })
      return chatMs === 0 ? answer : new Promise<Response>((resolve) => setTimeout(() => resolve(answer), chatMs))
    }
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

/** A GM with the campaign restored from a deep link, and (or not) a document named in it. */
const withWorkbench = (documentId: string | null): Decorator => {
  const Wrapped: Decorator = (Story) => (
    <CampaignProvider restore={{ campaignId: CAMPAIGN_ID, conversationId: THREAD_ID, documentId }}>
      <Story />
    </CampaignProvider>
  )
  return Wrapped
}

const meta = {
  title: 'Shell/WorkspaceShell/Workbench',
  component: WorkspaceShell,
  parameters: { layout: 'fullscreen' },
  beforeEach: workbenchApi(),
  decorators: [withShell({ mode: 'gm', role: 'dm' })],
} satisfies Meta<typeof WorkspaceShell>

export default meta
type Story = StoryObj<typeof meta>

// ── Geometry ─────────────────────────────────────────────────────────────────

function box(canvasElement: HTMLElement, selector: string): DOMRect {
  const element = canvasElement.querySelector(selector)
  if (element === null) throw new Error(`no ${selector}`)
  return element.getBoundingClientRect()
}

const near = (actual: number, expected: number, slack = 1): boolean => Math.abs(actual - expected) <= slack

async function openCanvas(canvasElement: HTMLElement): Promise<ReturnType<typeof within>> {
  const canvas = within(canvasElement)
  await canvas.findByRole('heading', { level: 2, name: TITLE })
  return canvas
}

/** C-1: over a long transcript the composer is on screen and the transcript is what scrolls. */
async function expectComposerOnScreen(canvasElement: HTMLElement): Promise<void> {
  // The thread loads after the shell renders: wait for its newest turn before measuring it.
  await within(canvasElement).findByText('Question number 40 about the harbour')
  const composer = box(canvasElement, '.chat-pane__composer')
  await expect(composer.top).toBeGreaterThanOrEqual(0)
  await expect(composer.bottom).toBeLessThanOrEqual(window.innerHeight + 1)
  const transcript = canvasElement.querySelector('.chat-pane__exchanges')
  if (transcript === null) throw new Error('no transcript')
  await expect(transcript.scrollHeight).toBeGreaterThan(transcript.clientHeight)
}

const dark = (story: Story, name: ViewportName): Story => ({
  ...story,
  ...atViewport(name, 'dark'),
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})

const documentOpen = { decorators: [withWorkbench(DOC_ID)] }

// ── Wide (LAYOUT-1) ──────────────────────────────────────────────────────────

/** 1280px: the 56px rail, the 472px chat, and the canvas taking the rest. */
export const Wide1280Canvas: Story = {
  ...documentOpen,
  ...atViewport('wide1280'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide1280')
    const canvas = await openCanvas(canvasElement)
    const rail = canvas.getByRole('navigation', { name: 'Navigation rail' }).getBoundingClientRect()
    await expect(rail.left).toBe(0)
    await expect(rail.width).toBe(56)
    const chat = box(canvasElement, '.workbench__chat')
    const column = box(canvasElement, '.workbench__canvas')
    await expect(near(chat.width, 472)).toBe(true)
    await expect(column.left).toBeGreaterThanOrEqual(chat.right - 1)
    await expect(column.width).toBeGreaterThan(600)
    await expect(canvas.queryByRole('group', { name: 'Workbench view' })).toBeNull()
    await expectComposerOnScreen(canvasElement)
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Open navigation', 'Close canvas'])
  },
}
export const Wide1280CanvasDark = dark(Wide1280Canvas, 'wide1280')

/** 1024px: the narrowest wide layout. 56 + 472 leaves 496px, so the canvas header goes compact. */
export const Wide1024Canvas: Story = {
  ...documentOpen,
  ...atViewport('wide1024'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide1024')
    const canvas = await openCanvas(canvasElement)
    const column = box(canvasElement, '.workbench__canvas')
    await expect(near(column.width, 1024 - 56 - 472, 2)).toBe(true)
    await expect(canvas.getByRole('region', { name: TITLE })).toHaveAttribute('data-layout', 'compact')
    await expectNoPageOverflow()
  },
}

// ── Medium (LAYOUT-2) ────────────────────────────────────────────────────────

/** 900px, Canvas view: one column; the chat is concealed, never hidden, so its status node stays exposed. */
export const Medium900Canvas: Story = {
  ...documentOpen,
  ...atViewport('medium900'),
  play: async ({ canvasElement }) => {
    await expectViewport('medium900')
    const canvas = await openCanvas(canvasElement)
    await expect(canvas.getByRole('navigation', { name: 'Navigation rail' }).getBoundingClientRect().width).toBe(56)
    await expect(canvas.getByRole('group', { name: 'Workbench view' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', 'true')
    await expect(box(canvasElement, '.workbench__chat').width).toBe(0)
    await expect(near(box(canvasElement, '.workbench__canvas').width, 900 - 56)).toBe(true)
    const arrival = canvasElement.querySelector('.workbench__chat .chat-pane__arrival')
    await expect(arrival).not.toBeNull()
    // Exposed means no ancestor is `display: none` either (`checkVisibility` walks them).
    await expect((arrival as Element).checkVisibility()).toBe(true)
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Chat', 'Canvas', 'Open navigation'])
  },
}
export const Medium900CanvasDark = dark(Medium900Canvas, 'medium900')

/** 900px, Chat view: the canvas column is gone from the layout (C-1: `[hidden]` must win over `display: flex`). */
export const Medium900Chat: Story = {
  ...documentOpen,
  ...atViewport('medium900'),
  play: async ({ canvasElement }) => {
    await expectViewport('medium900')
    const canvas = await openCanvas(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Chat' }))
    await expect(canvas.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', 'true')
    const column = canvasElement.querySelector('.workbench__canvas')
    await expect(column).toHaveAttribute('hidden')
    await expect((column as Element).getClientRects().length).toBe(0)
    await expect(near(box(canvasElement, '.workbench__chat').width, 900 - 56)).toBe(true)
    await expectComposerOnScreen(canvasElement)
    await expectNoPageOverflow()
  },
}

// ── Narrow (LAYOUT-3) ────────────────────────────────────────────────────────

/** 375px, Canvas view: no rail, the TopBar menu, the switch, and a canvas that runs edge to edge (C-15). */
export const Phone375Canvas: Story = {
  ...documentOpen,
  ...atViewport('phone375'),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = await openCanvas(canvasElement)
    await expect(canvas.queryByRole('navigation', { name: 'Navigation rail' })).toBeNull()
    await expect(canvas.getByRole('button', { name: 'Open navigation' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', 'true')
    const column = box(canvasElement, '.workbench__canvas')
    await expect(column.left).toBe(0)
    await expect(column.width).toBe(375)
    await expect(canvas.getByRole('button', { name: 'Back' })).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Chat', 'Canvas', 'Back', 'Open navigation'])
  },
}
export const Phone375CanvasDark = dark(Phone375Canvas, 'phone375')

/** 375px, Chat view: the canvas is out of the layout and the composer is on screen over a long transcript (C-1). */
export const Phone375Chat: Story = {
  ...documentOpen,
  ...atViewport('phone375'),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = await openCanvas(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Chat' }))
    const column = canvasElement.querySelector('.workbench__canvas') as Element
    await expect(column.getClientRects().length).toBe(0)
    const chat = box(canvasElement, '.workbench__chat')
    await expect(chat.width).toBe(375)
    await expectComposerOnScreen(canvasElement)
    await expectNoPageOverflow()
  },
}
export const Phone375ChatDark = dark(Phone375Chat, 'phone375')

/**
 * C-2: while the canvas shows, a turn the GM sent from the chat settles and is still
 * announced. The chat column is concealed (its children are `display: none`), not
 * `hidden` or inert, so its one status node stays exposed; and nothing focusable is
 * reachable in the concealed chat.
 */
export const Phone375ChatSettlesWhileCanvasShows: Story = {
  ...documentOpen,
  ...atViewport('phone375'),
  beforeEach: workbenchApi({ chatMs: 250 }),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = await openCanvas(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Chat' }))
    await userEvent.type(canvas.getByPlaceholderText('Ask…'), 'Who keeps the light?')
    await userEvent.keyboard('{Enter}')
    await userEvent.click(canvas.getByRole('button', { name: 'Canvas' }))
    const chat = canvasElement.querySelector('.workbench__chat') as HTMLElement
    await expect(chat).toHaveAttribute('data-concealed')
    const arrival = chat.querySelector('.chat-pane__arrival') as HTMLElement
    await waitFor(() => expect(arrival).toHaveTextContent('Answer received'))
    await expect(arrival.closest('[hidden], [inert]')).toBeNull()
    await expect(arrival.checkVisibility()).toBe(true)
    const reachable = Array.from(
      chat.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter((element) => element.getClientRects().length > 0)
    await expect(reachable).toEqual([])
  },
}

// ── A phone in landscape (C-15): medium by width, 375px tall ─────────────────

export const Medium812x375Canvas: Story = {
  ...documentOpen,
  ...atViewport('landscape812'),
  play: async ({ canvasElement }) => {
    await expectViewport('landscape812')
    const canvas = await openCanvas(canvasElement)
    await expect(canvas.getByRole('group', { name: 'Workbench view' })).toBeVisible()
    await expect(box(canvasElement, '.gm-canvas__body').height).toBeGreaterThanOrEqual(120)
    await expectNoPageOverflow()
  },
}

export const Medium812x375Chat: Story = {
  ...documentOpen,
  ...atViewport('landscape812'),
  play: async ({ canvasElement }) => {
    await expectViewport('landscape812')
    const canvas = await openCanvas(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Chat' }))
    const composer = box(canvasElement, '.chat-pane__composer')
    await expect(composer.top).toBeGreaterThanOrEqual(0)
    await expect(composer.bottom).toBeLessThanOrEqual(window.innerHeight + 1)
    await expectNoPageOverflow()
    await expectNothingClipped(canvasElement)
  },
}

// ── Keyboard (C-14) ──────────────────────────────────────────────────────────

/** The skip links are the first stops, visible on focus, and land on the conversation and on the document. */
export const SkipLinksByKeyboard: Story = {
  ...documentOpen,
  ...atViewport('wide1280'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide1280')
    const canvas = await openCanvas(canvasElement)
    const toConversation = canvas.getByRole('button', { name: 'Skip to conversation' })
    const toDocument = canvas.getByRole('button', { name: 'Skip to document' })
    await tabTo(toConversation, 3)
    const shown = toConversation.getBoundingClientRect()
    await expect(shown.top).toBeGreaterThanOrEqual(0)
    await expect(shown.right).toBeLessThanOrEqual(window.innerWidth)
    await userEvent.keyboard('{Enter}')
    await expect(canvas.getByRole('region', { name: 'Conversation' })).toHaveFocus()
    await tabTo(toDocument, 40)
    await userEvent.keyboard('{Enter}')
    await expect(canvas.getByRole('heading', { level: 2, name: TITLE })).toHaveFocus()
  },
}

// ── Where focus lands when the canvas closes (C-3, CANVAS-32), measured with real visibility ──

/** 1280: a sidebar row opens the document and the sidebar steps aside; closing brings it back and focuses that row. */
export const FocusReturnsToSidebarRow: Story = {
  ...atViewport('wide1280'),
  decorators: [withWorkbench(null)],
  play: async ({ canvasElement }) => {
    await expectViewport('wide1280')
    const canvas = within(canvasElement)
    const row = await canvas.findByRole('button', { name: new RegExp(TITLE) })
    await userEvent.click(row)
    await canvas.findByRole('heading', { level: 2, name: TITLE })
    // The row is connected but its sidebar is gone from the layout: the case a plain isConnected check misses.
    await waitFor(() => expect(row.checkVisibility()).toBe(false))
    await userEvent.click(canvas.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: new RegExp(TITLE) })).toHaveFocus())
    await expect(canvas.getByRole('navigation', { name: 'Main navigation' })).toBeVisible()
  },
}

/** 900: a row in the drawer opens the document; closing returns focus to the rail's Open navigation, which opened the drawer. */
export const FocusReturnsToRailFromDrawerRow: Story = {
  ...atViewport('medium900'),
  decorators: [withWorkbench(null)],
  play: async ({ canvasElement }) => {
    await expectViewport('medium900')
    const canvas = within(canvasElement)
    await userEvent.click(await canvas.findByRole('button', { name: 'Open navigation' }))
    await userEvent.click(await canvas.findByRole('button', { name: new RegExp(TITLE) }))
    await canvas.findByRole('heading', { level: 2, name: TITLE })
    await userEvent.click(canvas.getByRole('button', { name: 'Close canvas' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Open navigation' })).toHaveFocus())
  },
}

/** 375: a link in the chat opens the Canvas view; Back returns focus to that link. */
export const FocusReturnsToChatLinkOnPhone: Story = {
  ...atViewport('phone375'),
  decorators: [withWorkbench(null)],
  beforeEach: workbenchApi({ turns: 2, toolLink: true }),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = within(canvasElement)
    const link = await canvas.findByRole('button', { name: new RegExp(LINK_TITLE) })
    await userEvent.click(link)
    await canvas.findByRole('heading', { level: 2, name: TITLE })
    await expect(canvas.getByRole('button', { name: 'Canvas' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(canvas.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: new RegExp(LINK_TITLE) })).toHaveFocus())
  },
}
