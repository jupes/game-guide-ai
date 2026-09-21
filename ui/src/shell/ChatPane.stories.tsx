/**
 * ChatPane — the signed-in product. Everything a user actually does happens
 * here, and until agent-forge-harness-27h none of it had a story.
 *
 * Every seam is a prop, so nothing here touches the network: `post`,
 * `loadHistory`, `getAttachments` and `uploadAttachment` are all injected.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { tabTo } from '../../.storybook/keyboard'
import { withShell } from '../../.storybook/shellHarness'
import { ChatPane } from './ChatPane'
import type { GetAttachmentsFn } from './ChatPane'
import type { Attachment, ChatResponse, ChatResult, Source, StoredMessage } from '../api'
import type { LoadHistoryFn, PostFn } from '../useChat'

// ── Fixtures ─────────────────────────────────────────────────────────────────

const SOURCES: Source[] = [
  {
    book: "Player's Handbook",
    chapter: 'Chapter 11: Spells',
    section: 'Shield',
    entity: 'Shield',
    page: 275,
    snippet: 'An invisible barrier of magical force appears and protects you.',
  },
  {
    book: "Player's Handbook",
    chapter: 'Chapter 9: Combat',
    section: 'Reactions',
    entity: null,
    page: 190,
    snippet: 'Certain special abilities, spells, and situations allow you to take a special action called a reaction.',
  },
]

function answer(over: Partial<ChatResponse> = {}): ChatResponse {
  return {
    answer:
      'Shield is a **reaction**, cast when you are hit by an attack or targeted by *magic missile*. It gives +5 AC, including against the triggering attack.',
    sources: SOURCES,
    answerable: true,
    ...over,
  }
}

// Each seam gets a typed factory. `satisfies Meta<…>` narrows whatever the
// meta's own args happen to be, so an inline arrow in a story would be checked
// against that narrowed literal rather than against the prop's real union.
const never: PostFn = () => new Promise<ChatResult>(() => {})
const neverHistory: LoadHistoryFn = () => new Promise(() => {})

function answers(result: ChatResult): PostFn {
  return async () => result
}

function history(messages: StoredMessage[]): LoadHistoryFn {
  return async () => ({ kind: 'ok', messages })
}

function historyFails(message: string): LoadHistoryFn {
  return async () => ({ kind: 'error', message })
}

function attached(rows: Attachment[]): GetAttachmentsFn {
  return async () => ({ kind: 'ok', attachments: rows })
}

function turn(id: number, prompt: string, reply: string): StoredMessage[] {
  return [
    { id, role: 'user', content: prompt, mode: 'sage', created_at: '2026-09-18T19:02:00Z' },
    { id: id + 1, role: 'assistant', content: reply, mode: 'sage', created_at: '2026-09-18T19:02:04Z' },
  ]
}

function attachment(over: Partial<Attachment> = {}): Attachment {
  return {
    id: 1,
    filename: 'session-14-notes.md',
    content_type: 'text/markdown',
    chars: 4_210,
    created_at: '2026-09-18T18:00:00Z',
    ...over,
  }
}

const meta = {
  title: 'Shell/ChatPane',
  component: ChatPane,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  args: {
    post: answers({ kind: 'ok', response: answer() }),
    loadHistory: history([]),
    getAttachments: attached([]),
    uploadAttachment: async () => ({ kind: 'ok', attachment: attachment() }),
  },
  decorators: [
    // The pane is `height: 100%` and its feed is the scroller, so it needs a
    // bounded parent exactly as WorkspaceShell gives it. Without one the feed
    // grows instead of scrolling, and the autoscroll states cannot happen.
    (Story) => (
      <div style={{ height: 480, display: 'flex' }}>
        <Story />
      </div>
    ),
    withShell({ conversations: [{ mode: 'sage' }], selected: 0 }),
  ],
} satisfies Meta<typeof ChatPane>

export default meta
type Story = StoryObj<typeof meta>

// ── Empty and loading ────────────────────────────────────────────────────────

/** Nothing said yet. The prompt is per-channel, so it names who you are asking. */
export const EmptySage: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('Ask the Sage…')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Send message' })).toBeDisabled()
  },
}

export const EmptyGmChannel: Story = {
  decorators: [withShell({ mode: 'gm', conversations: [{ mode: 'gm' }], selected: 0 })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('Ask the Game Master…')).toBeInTheDocument()
  },
}

/** Recalling stored history. A status, not a spinner with no words. */
export const LoadingHistory: Story = {
  args: { loadHistory: neverHistory },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('status')).toHaveTextContent('Recalling the conversation…')
  },
}

/** Recall failed. Recoverable: the thread just starts empty, and says why. */
export const HistoryUnavailable: Story = {
  args: { loadHistory: historyFails("Couldn't load this conversation's history.") },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(
      await canvas.findByText("Couldn't load this conversation's history."),
    ).toBeInTheDocument()
  },
}

/** Recalled history, rendered as prior turns. */
export const WithRecalledHistory: Story = {
  args: {
    loadHistory: history([
      ...turn(1, 'How does grappling work?', 'Grappling is an attack replacement: you make a Strength (Athletics) check contested by the target.'),
      ...turn(3, 'Can I grapple something larger?', 'Only up to one size larger than you.'),
    ]),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('How does grappling work?')).toBeInTheDocument()
    await expect(canvas.getByText(/only up to one size larger/i)).toBeInTheDocument()
  },
}

/**
 * agent-forge-harness-27h, rework 1 — the transcript is a NAMED REGION, and
 * deliberately not a live region.
 *
 * The measured defect was `scrollable-region-focusable`: the feed is the
 * scroller and nothing inside it was focusable, so a keyboard-only reader could
 * not scroll back through their own conversation. The fix is a tab stop plus
 * the accessible name a focusable region needs — `role="region"` +
 * `aria-label`, and nothing beyond that.
 *
 * It is NOT `role="log"`. `log` carries an implicit `aria-live="polite"` over
 * everything inside it, the user's own prompts included; and because
 * `WorkspaceShell` mounts `ChatPane` with no `key` while `useChat` swaps
 * `exchanges` in place, switching conversations would MUTATE that live region
 * rather than remount it — a recalled history announced as though it had just
 * arrived. Axe has no rule for any of this in either direction, so the role is
 * pinned here by name: put `log` back and this story is the only thing in the
 * repository that goes red.
 */
export const TranscriptIsANamedRegionNotALiveRegion: Story = {
  args: {
    loadHistory: history(
      turn(1, 'What does a shield spell stop?', 'The triggering attack, and *magic missile*.'),
    ),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await canvas.findByText('What does a shield spell stop?')

    const feed = canvas.getByRole('region', { name: 'Conversation' })
    await expect(feed).toHaveClass('chat-pane__exchanges')
    await expect(feed).toHaveAttribute('role', 'region')
    await expect(feed).toHaveAttribute('tabindex', '0')

    // No live region: not implicitly (the role is `region`, not `log` or
    // `status`) and not explicitly either.
    await expect(canvasElement.querySelector('[role="log"]')).toBeNull()
    await expect(canvasElement.querySelector('[aria-live]')).toBeNull()

    // …and the stop is a real one. `tabTo` uses Tab presses, so this fails if
    // the transcript ever drops back out of the tab order.
    await tabTo(feed)
  },
}

// ── Sending ──────────────────────────────────────────────────────────────────

/**
 * Sent by keyboard alone: Tab to the composer, type, press Enter. Enter sends
 * and Shift+Enter is a newline — the composer contract.
 *
 * Reached with real Tab presses (`tabTo`), not `field.focus()`: "by keyboard
 * alone" is a claim about the TAB ORDER, and a scripted `.focus()` would keep
 * this story green on a composer no keyboard could get to.
 */
export const SentByKeyboard: Story = {
  args: { post: fn(async () => ({ kind: 'ok', response: answer() })) as PostFn },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    await tabTo(field)

    await userEvent.keyboard('Does shield stop magic missile?{Enter}')

    await expect(args.post).toHaveBeenCalledTimes(1)
    await expect(canvas.getByText('Does shield stop magic missile?')).toBeInTheDocument()
    await expect(await canvas.findByText('2 sources')).toBeInTheDocument()
    // The draft is cleared, so the next question starts empty.
    await expect(field).toHaveValue('')
  },
}

/** Shift+Enter writes a newline instead of sending. */
export const ShiftEnterIsANewline: Story = {
  args: { post: fn(async () => ({ kind: 'ok', response: answer() })) as PostFn },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('first line{Shift>}{Enter}{/Shift}second line')
    await expect(args.post).not.toHaveBeenCalled()
    await expect(field).toHaveValue('first line\nsecond line')
  },
}

/**
 * The turn is in flight. The animated dots are decoration and aria-hidden; the
 * status text is the real affordance and stays for assistive tech.
 */
export const AwaitingAnswer: Story = {
  args: { post: never },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('What happens on a critical hit?{Enter}')
    await expect(await canvas.findByText('Consulting the tomes…')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Send message' })).toBeDisabled()
  },
}

/** The service refused. The failure is attached to the turn that caused it. */
export const AnswerFailed: Story = {
  args: {
    post: answers({
      kind: 'error',
      message: 'The service is busy right now — try again in a moment.',
      outcome: 'throttled',
      retryable: true,
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('Anything?{Enter}')
    await expect(
      await canvas.findByText('The service is busy right now — try again in a moment.'),
    ).toBeInTheDocument()
  },
}

// ── Answer shapes ────────────────────────────────────────────────────────────

/** Spell channel: prose, the structured card and the three usage suggestions. */
export const SpellAnswer: Story = {
  decorators: [withShell({ mode: 'spell', conversations: [{ mode: 'spell' }], selected: 0 })],
  args: {
    post: answers({
      kind: 'ok',
      response: answer({
        answer: '**Shield** is a 1st-level abjuration, cast as a reaction.',
        spell_content: {
          name: 'Shield',
          level: 1,
          school: 'Abjuration',
          casting_time: '1 reaction',
          range: 'Self',
          duration: '1 round',
          components: { v: true, s: true, m: null },
          description: 'An invisible barrier of magical force appears and protects you.',
          higher_levels: null,
          classes: ['Sorcerer', 'Wizard'],
          concentration: false,
          ritual: false,
        },
        suggestions: [
          { style: 'practical', text: 'Hold your reaction when you expect a rogue’s sneak attack.' },
          { style: 'roleplay', text: 'Describe the barrier in your character’s own sigil.' },
          { style: 'wacky', text: 'Catch a thrown pie. It still counts.' },
        ],
      }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('Tell me about shield{Enter}')
    await expect(await canvas.findByText('Practical')).toBeInTheDocument()
    await expect(canvas.getByText('Wacky')).toBeInTheDocument()
  },
}

/**
 * GM channel, unanswerable from the corpus: the answer is invented, and it is
 * labelled as invented rather than passed off as grounded.
 */
export const GmCreativeAnswer: Story = {
  decorators: [withShell({ mode: 'gm', conversations: [{ mode: 'gm' }], selected: 0 })],
  args: {
    post: answers({
      kind: 'ok',
      response: answer({
        answer: 'The innkeeper is a retired sapper who lost three fingers to a glyph she still will not describe.',
        sources: [],
        answerable: false,
      }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('Who runs the inn?{Enter}')
    await expect(await canvas.findByText(/Creative — may include invented content/)).toBeInTheDocument()
  },
}

/** Dice notation in an answer is lifted out into a DiceRoll. */
export const DiceInTheAnswer: Story = {
  args: {
    post: answers({
      kind: 'ok',
      // The parser wants the model's reported total, not just the notation.
      response: answer({ answer: 'You rolled 1d20+3 = 18 for the Stealth check.', sources: [] }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('Roll stealth{Enter}')
    await expect(await canvas.findByText('d20 + 3')).toBeInTheDocument()
  },
}

/** A thread long enough to scroll, which is where the autoscroll rule lives. */
export const LongConversation: Story = {
  args: {
    loadHistory: history(
      Array.from({ length: 10 }, (_, i) =>
        turn(
          i * 2 + 1,
          `Question ${i + 1}: what happens if the party ignores the obvious warning?`,
          `Answer ${i + 1}. ${'The corridor narrows, and the air tastes of iron. '.repeat(4)}`,
        ),
      ).flat(),
    ),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText(/Answer 10\./)).toBeInTheDocument()
  },
}

/**
 * The reader has scrolled back through history. "Jump to latest" is a real
 * button, not a floating decoration, so it is reachable and announced.
 */
export const ScrolledAwayFromLatest: Story = {
  args: {
    loadHistory: history(
      Array.from({ length: 10 }, (_, i) =>
        turn(i * 2 + 1, `Question ${i + 1}`, `Answer ${i + 1}. ${'Long enough to scroll. '.repeat(10)}`),
      ).flat(),
    ),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await canvas.findByText(/Answer 10\./)
    const feed = canvasElement.querySelector('.chat-pane__exchanges')
    if (feed instanceof HTMLElement) {
      feed.scrollTop = 0
      feed.dispatchEvent(new Event('scroll'))
    }
    const jump = await canvas.findByRole('button', { name: /jump to latest/i })
    jump.focus()
    await userEvent.keyboard('{Enter}')
    await expect(canvas.queryByRole('button', { name: /jump to latest/i })).not.toBeInTheDocument()
  },
}

// ── Attachments ──────────────────────────────────────────────────────────────

/** Files already attached to this conversation, as metadata chips. */
export const WithAttachments: Story = {
  args: {
    getAttachments: attached([
      attachment(),
      attachment({ id: 2, filename: 'the-drowned-shrine.pdf', content_type: 'application/pdf' }),
    ]),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('session-14-notes.md')).toBeInTheDocument()
    await expect(canvas.getByText('the-drowned-shrine.pdf')).toBeInTheDocument()
  },
}

/** No conversation open: there is nothing to attach a file to. */
export const AttachDisabledWithoutConversation: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Attach file' })).toBeDisabled()
  },
}

// ── Dark ─────────────────────────────────────────────────────────────────────

export const Dark: Story = {
  globals: { theme: 'dark' },
  args: {
    loadHistory: history(
      turn(1, 'What does a shield spell stop?', 'It stops the triggering attack, and magic missile outright.'),
    ),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('What does a shield spell stop?')).toBeInTheDocument()
  },
}

export const DarkEmpty: Story = {
  globals: { theme: 'dark' },
}

export const DarkAnswerFailed: Story = {
  globals: { theme: 'dark' },
  args: { post: answers({ kind: 'error', message: 'Network unreachable.' }) },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const field = await canvas.findByRole('textbox')
    field.focus()
    await userEvent.keyboard('Anything?{Enter}')
    await expect(await canvas.findByText('Network unreachable.')).toBeInTheDocument()
  },
}
