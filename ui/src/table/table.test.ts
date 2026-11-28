import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import html from '../../table.html?raw'
import { tableAddress, tableCampaign } from './address'
import { mountTable } from './table'
import type { IdentityChannelLike } from '../shell/identityBroadcast'

const event = { schema_version: 1 }
const slot = (name: 'table' | 'mine', title: string | null, seq = 1) => ({
  slot: name, seq, content: title === null ? null : {
    content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: title }],
    gm_note: 'must never surface',
  },
})
const picture = (role: 'participant' | 'guest', ...slots: ReturnType<typeof slot>[]) => Response.json({
  ...event,
  frames: [
    { ...event, event: 'session', role, audio: false },
    { ...event, event: 'snapshot', slots },
    { ...event, event: 'ready' },
  ],
})
const content = () => document.querySelector('#table-projections')!
const status = () => document.querySelector('#table-status')!.textContent

let stop: (() => void) | undefined
let visibility: ReturnType<typeof vi.fn<() => DocumentVisibilityState>>

beforeEach(() => {
  vi.useFakeTimers()
  visibility = vi.fn<() => DocumentVisibilityState>().mockReturnValue('visible')
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(visibility)
  document.body.innerHTML = new DOMParser().parseFromString(html, 'text/html').body.innerHTML
})

afterEach(() => {
  stop?.()
  stop = undefined
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
  document.body.replaceChildren()
})

function start(request: typeof fetch, channel?: IdentityChannelLike, stream?: typeof fetch) {
  stop = mountTable(document, 'cmp_table', (input, init) => String(input).startsWith('/table/stream?')
    ? (stream?.(input, init) ?? Promise.resolve(new Response(null, { status: 404 })))
    : request(input, init), () => channel ?? null)
  return vi.advanceTimersByTimeAsync(0)
}

describe('account-authorized player table', () => {
  it('normalizes the address to a single opaque campaign ID', () => {
    expect(tableAddress('cmp_table')).toBe('/table?campaign_id=cmp_table')
    window.history.replaceState(null, '', '/table?campaign_id=cmp_table&seat=secret#ignored')
    expect(tableCampaign(window.location, window.history)).toBe('cmp_table')
    expect(window.location.search).toBe('?campaign_id=cmp_table')
    expect(window.location.hash).toBe('')
    window.history.replaceState(null, '', '/table?campaign_id=one&campaign_id=two')
    expect(tableCampaign(window.location, window.history)).toBeNull()
    expect(window.location.search).toBe('')
  })

  it('shows only the two validated slots, with private content first and revealed strings as plain text', async () => {
    const literal = '<img src=bad onerror="alert(1)">'
    const request = vi.fn(async () => picture('participant', slot('table', literal), slot('mine', 'My character')))
    await start(request)
    expect([...content().querySelectorAll('h2')].map((node) => node.textContent)).toEqual(['For you', 'Shared with the table'])
    expect(content()).toHaveTextContent(literal)
    expect(content()).toHaveTextContent('My character')
    expect(content()).not.toHaveTextContent('must never surface')
    expect(content().querySelector('img, a, [onerror]')).toBeNull()
    expect(request).toHaveBeenCalledWith('/table/snapshot?campaign_id=cmp_table', {
      cache: 'no-store', signal: expect.any(AbortSignal),
    })
  })

  it.each([401, 404])('shows the generic %s state and never displays the body of a refusal', async (code) => {
    const request = vi.fn(async () => Response.json({ secret: 'Refused data' }, { status: code }))
    await start(request)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toBe(code === 401 ? 'Sign in to open this table.' : 'This table isn’t active or available to you.')
    expect(document.querySelector('#table-sign-in')).toHaveProperty('hidden', code !== 401)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('refuses an own-slot projection without the participant role', async () => {
    await start(async () => picture('guest', slot('table', 'Public'), slot('mine', 'Private')))
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toMatch(/hidden/)
  })

  it('changes a live table to waiting, announces new content, and clears immediately on inactivity', async () => {
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', null)))
      .mockResolvedValueOnce(picture('participant', slot('table', 'Shared handout'), slot('mine', null)))
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
    await start(request)
    expect(status()).toMatch(/Nothing to see yet/)
    await vi.advanceTimersByTimeAsync(5000)
    expect(status()).toBe('Your GM shared something new.')
    expect(content()).toHaveTextContent('Shared handout')
    await vi.advanceTimersByTimeAsync(5000)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toMatch(/isn’t active/)
  })

  it('blanks after ten seconds without a confirmed refresh, even if the next request hangs', async () => {
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('guest', slot('table', 'Old handout')))
      .mockImplementation(() => new Promise(() => {}))
    await start(request)
    expect(content()).toHaveTextContent('Old handout')
    await vi.advanceTimersByTimeAsync(9999)
    expect(content()).toHaveTextContent('Old handout')
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toMatch(/hidden/)
  })

  it('clears on phone lock and discards an old response before accepting a fresh one', async () => {
    let answerOld!: (response: Response) => void
    let answerFresh!: (response: Response) => void
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('guest', slot('table', 'Before lock')))
      .mockImplementationOnce(() => new Promise((resolve) => { answerOld = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { answerFresh = resolve }))
    await start(request)
    await vi.advanceTimersByTimeAsync(5000)
    visibility.mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(content()).toBeEmptyDOMElement()
    expect(request.mock.calls[1][1]?.signal?.aborted).toBe(true)
    visibility.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    answerOld(picture('guest', slot('table', 'Stale response')))
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    answerFresh(picture('guest', slot('table', 'After unlock')))
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('After unlock')
    expect(content()).not.toHaveTextContent('Stale response')
  })

  it('clears on a cross-tab identity change and broadcasts its own sign-out', async () => {
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', 'Previous account')))
      .mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', 'Current account')))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    await start(request, channel)
    expect(content()).toHaveTextContent('Previous account')
    channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    expect(content()).toBeEmptyDOMElement()
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Current account')
    const signOut = document.querySelector<HTMLButtonElement>('#table-sign-out')!
    signOut.focus()
    signOut.click()
    expect(content()).toBeEmptyDOMElement()
    await vi.advanceTimersByTimeAsync(0)
    expect(request).toHaveBeenLastCalledWith('/auth/logout', { method: 'POST', cache: 'no-store' })
    expect(channel.postMessage).toHaveBeenCalledWith({ v: 1, kind: 'identity-changed' })
    expect(document.querySelector('#table-sign-in')).toHaveFocus()
  })
})

function streamFixture() {
  const encoder = new TextEncoder()
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const cancel = vi.fn()
  const body = new ReadableStream<Uint8Array>({ start: (value) => { controller = value }, cancel })
  const response = new Response(body, { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
  const chunk = (value: string | Uint8Array) => controller.enqueue(typeof value === 'string' ? encoder.encode(value) : value)
  const frame = (name: string, fields = {}) => chunk(`event: ${name}\ndata: ${JSON.stringify({
    ...event, ...fields, event: name,
  })}\n\n`)
  const snapshot = (role: 'guest' | 'participant', slots: ReturnType<typeof slot>[]) => {
    frame('session', { role, audio: false })
    frame('snapshot', { slots })
  }
  return { response, chunk, frame, snapshot, cancel }
}

const connection = () => document.querySelector('#table-connection')!

const sharedNotice = 'Your GM shared something new.'
const stoppedNotice = 'Your GM stopped sharing something.'

function watchStatus() {
  const region = document.querySelector<HTMLElement>('#table-status')!
  expect(region).toHaveAttribute('role', 'status')
  const messages: string[] = []
  const observer = new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.textContent) messages.push(node.textContent)
      }
      if (record.type === 'characterData' && record.target.textContent) messages.push(record.target.textContent)
    }
  })
  observer.observe(region, { childList: true, characterData: true, subtree: true })
  return { messages, disconnect: () => observer.disconnect() }
}

describe('player table announcements and focus', () => {
  it('announces both slots in a new snapshot once, and stays quiet on equal polls and the initial picture', async () => {
    const opened = [slot('table', 'First table secret'), slot('mine', 'First personal secret')]
    const changed = [slot('table', 'Second table secret', 2), slot('mine', 'Second personal secret', 2)]
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', ...opened))
      .mockResolvedValueOnce(picture('participant', ...changed))
      .mockImplementation(async () => picture('participant', ...changed))
    const live = watchStatus()
    try {
      await start(request)
      expect(content()).toHaveTextContent('First personal secret')
      expect(live.messages).not.toContain(sharedNotice)
      await vi.advanceTimersByTimeAsync(10_500)
      expect(request).toHaveBeenCalledTimes(3)
      expect(content()).toHaveTextContent('Second personal secret')
      expect(live.messages.filter(message => message === sharedNotice)).toHaveLength(1)
      expect(live.messages.join(' ')).not.toMatch(/secret|personal|For you/i)
    } finally {
      live.disconnect()
    }
  })

  it('announces each visible disclosure and a partial Stop, ignoring duplicate, audio-only and unrenderable updates', async () => {
    const stream = streamFixture()
    await start(vi.fn(), undefined, async () => stream.response)
    stream.frame('session', { role: 'participant', audio: true })
    stream.frame('snapshot', { slots: [slot('table', 'Handout'), slot('mine', 'Personal note')] })
    stream.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    const live = watchStatus()
    try {
      stream.frame('slot', slot('table', 'A replacement', 2))
      stream.frame('slot', slot('table', 'A replacement', 3))
      stream.frame('slot', slot('table', 'A replacement', 3))
      stream.frame('audio', { slot: 'ambience', seq: 1, playing: null })
      await vi.advanceTimersByTimeAsync(600)
      expect(live.messages).toEqual([sharedNotice, sharedNotice])
      stream.frame('slot', slot('mine', null, 2))
      await vi.advanceTimersByTimeAsync(600)
      expect(content()).toHaveTextContent('A replacement')
      expect(content()).not.toHaveTextContent('Personal note')
      expect(live.messages).toEqual([sharedNotice, sharedNotice, stoppedNotice])
      stream.frame('slot', { slot: 'mine', seq: 3, content: {
        content_kind: 'document', type: 'npc', fields: [{ key: 'portrait', value: {
          handle: 'a'.repeat(32), kind: 'image', media_type: 'image/webp', width: 10, height: 10, duration_ms: null,
        } }],
      } })
      await vi.advanceTimersByTimeAsync(600)
      expect(content()).not.toHaveTextContent('For you')
      expect(live.messages).toEqual([sharedNotice, sharedNotice, stoppedNotice])
    } finally {
      live.disconnect()
    }
  })

  it.each(['hidden', 'inactive', 'teardown'])('drops delayed reveal announcements after %s', async (transition) => {
    const stream = streamFixture()
    await start(vi.fn(), undefined, async () => stream.response)
    stream.snapshot('guest', [slot('table', 'Original')])
    stream.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    stream.frame('slot', slot('table', 'Second', 2))
    await vi.advanceTimersByTimeAsync(200)
    expect(status()).toBe(sharedNotice)
    stream.frame('slot', slot('table', 'Third', 3))
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Third')
    expect(status()).toBe('')
    const live = watchStatus()
    try {
      if (transition === 'hidden') {
        visibility.mockReturnValue('hidden')
        document.dispatchEvent(new Event('visibilitychange'))
      } else if (transition === 'inactive') {
        stream.frame('inactive')
      } else {
        stop!()
        stop = undefined
      }
      await vi.advanceTimersByTimeAsync(600)
      expect(live.messages).not.toContain(sharedNotice)
      expect(content()).toBeEmptyDOMElement()
      if (transition === 'inactive') expect(status()).toMatch(/isn’t active/)
    } finally {
      live.disconnect()
    }
  })

  it.each([
    { selector: '#table-reload', response: 401 },
    { selector: 'a[href="/tavern"]', response: 401 },
    { selector: '#table-sign-out', response: 404 },
  ])('preserves surviving focus on $selector after access is lost', async ({ selector, response }) => {
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', slot('table', 'Handout'), slot('mine', null)))
      .mockResolvedValueOnce(new Response(null, { status: response }))
    await start(request)
    const focused = document.querySelector<HTMLElement>(selector)!
    focused.focus()
    await vi.advanceTimersByTimeAsync(5000)
    expect(content()).toBeEmptyDOMElement()
    expect(focused).toBeVisible()
    expect(focused).toHaveFocus()
  })

  it('transfers focus from the disappearing account action to the table heading on authorization loss', async () => {
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', null)))
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
    await start(request)
    document.querySelector<HTMLButtonElement>('#table-sign-out')!.focus()
    await vi.advanceTimersByTimeAsync(5000)
    expect(document.querySelector('#table-sign-out')).not.toBeVisible()
    expect(document.querySelector('h1')).toHaveFocus()
  })

  it('leaves focus on the Tavern link if the user tabs away while sign-out is pending', async () => {
    let finishSignOut!: (response: Response) => void
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', null)))
      .mockImplementationOnce(() => new Promise(resolve => { finishSignOut = resolve }))
    await start(request)
    const signOut = document.querySelector<HTMLButtonElement>('#table-sign-out')!
    const tavern = document.querySelector<HTMLAnchorElement>('a[href="/tavern"]')!
    signOut.focus()
    signOut.click()
    expect(signOut).toHaveFocus()
    expect(signOut).toHaveAttribute('aria-disabled', 'true')
    signOut.click()
    tavern.focus()
    finishSignOut(new Response(null, { status: 204 }))
    await vi.advanceTimersByTimeAsync(0)
    expect(request.mock.calls.filter(([url]) => url === '/auth/logout')).toHaveLength(1)
    expect(status()).toBe('You signed out.')
    expect(tavern).toHaveFocus()
  })
})

describe('player table streams and reconnects', () => {
  it.each([
    { refusal: 401, reopen: 401, transport: 'stream' },
    { refusal: 403, reopen: 403, transport: 'stream' },
    { refusal: 404, reopen: 401, transport: 'snapshot' },
    { refusal: 403, reopen: 403, transport: 'snapshot' },
    { refusal: 404, reopen: 200, transport: 'snapshot' },
  ])('reauthorizes a previously live page after presence returns $refusal ($transport $reopen)', async ({ refusal, reopen, transport }) => {
    const live = streamFixture()
    live.response.headers.set('X-Table-Audio-Context', 'c'.repeat(64))
    const streams = vi.fn<typeof fetch>().mockResolvedValueOnce(live.response)
      .mockResolvedValue(new Response(null, { status: transport === 'stream' ? reopen : 404 }))
    let refuse!: (response: Response) => void
    const request = vi.fn<typeof fetch>().mockImplementation(async input => {
      if (String(input).startsWith('/table/presence?')) return new Promise(resolve => { refuse = resolve })
      return reopen === 200 ? picture('participant', slot('table', 'Fresh handout'), slot('mine', null))
        : new Response(null, { status: reopen })
    })
    await start(request, undefined, streams)
    live.frame('session', { role: 'guest', audio: true })
    live.frame('snapshot', { slots: [slot('table', 'Old handout')] })
    live.frame('ready')
    await vi.advanceTimersByTimeAsync(2)
    expect(content()).toHaveTextContent('Old handout')

    refuse(new Response(null, { status: refusal }))
    await vi.advanceTimersByTimeAsync(2)
    expect(live.cancel).toHaveBeenCalledOnce()
    expect(content()).not.toHaveTextContent('Old handout')
    expect(document.querySelector('#table-sign-in')).toHaveProperty('hidden', true)
    if (reopen === 200) expect(content()).toHaveTextContent('Fresh handout')
    else {
      expect(status()).toBe('This table isn’t active or available to you.')
      expect(document.querySelector('#table-sound section')).toHaveProperty('hidden', true)
      await vi.advanceTimersByTimeAsync(15_000)
      expect(streams).toHaveBeenCalledTimes(2)
    }
  })

  it('waits for ready and applies Stop per slot while rejecting stale or duplicate frames', async () => {
    const stream = streamFixture()
    const request = vi.fn()
    const open = vi.fn(async () => stream.response)
    await start(request, undefined, open)
    expect(open).toHaveBeenCalledWith('/table/stream?campaign_id=cmp_table', {
      cache: 'no-store', headers: { Accept: 'text/event-stream' }, signal: expect.any(AbortSignal),
    })
    stream.snapshot('participant', [slot('table', 'Shared'), slot('mine', 'Private')])
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    stream.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Shared')
    expect(content()).toHaveTextContent('Private')
    expect(connection()).toBeEmptyDOMElement()

    stream.frame('slot', { ...slot('table', null), seq: 2 })
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).not.toHaveTextContent('Shared')
    expect(content()).toHaveTextContent('Private')
    stream.frame('slot', { ...slot('table', 'Duplicate'), seq: 2 })
    stream.frame('slot', slot('table', 'Stale'))
    stream.frame('slot', { ...slot('mine', 'Changed privately'), seq: 2 })
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).not.toHaveTextContent(/Duplicate|Stale/)
    expect(content()).toHaveTextContent('Changed privately')
    stream.frame('inactive')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toMatch(/isn’t active/)
    expect(request).not.toHaveBeenCalled()
  })

  it('opens a new stream after a sequence gap and accepts equal marks only after its fresh ready', async () => {
    const initial = streamFixture()
    const reassigned = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(initial.response).mockResolvedValueOnce(reassigned.response)
    await start(vi.fn(), undefined, open)
    initial.snapshot('guest', [slot('table', 'Before gap')])
    initial.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Before gap')
    initial.frame('slot', { ...slot('table', 'Never show this gap'), seq: 4 })
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).toBeEmptyDOMElement()
    expect(open).toHaveBeenCalledTimes(2)
    expect(initial.cancel).toHaveBeenCalledOnce()
    reassigned.snapshot('guest', [slot('table', 'Fresh picture at the old mark')])
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    reassigned.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Fresh picture at the old mark')
  })

  it('uses comment bytes for liveness and blanks after ten seconds of silence', async () => {
    const live = streamFixture()
    await start(() => new Promise(() => {}), undefined, async () => live.response)
    live.snapshot('guest', [slot('table', 'The handout')])
    live.frame('ready')
    await vi.advanceTimersByTimeAsync(5000)
    live.chunk(': heartbeat\r')
    live.chunk('\n\r\n')
    await vi.advanceTimersByTimeAsync(9999)
    expect(content()).toHaveTextContent('The handout')
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).toBeEmptyDOMElement()
    expect(live.cancel).toHaveBeenCalled()
    expect(connection()).toHaveTextContent('Live updates are delayed')
  })

  it('clears on phone lock and waits for the new stream after unlock', async () => {
    const locked = streamFixture()
    const resumed = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(locked.response).mockResolvedValueOnce(resumed.response)
    await start(vi.fn(), undefined, open)
    locked.snapshot('guest', [slot('table', 'Old handout')])
    locked.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Old handout')
    locked.frame('slot', { ...slot('table', 'Buffered while locked'), seq: 2 })
    visibility.mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(content()).toBeEmptyDOMElement()
    expect(open.mock.calls[0][1]?.signal?.aborted).toBe(true)
    visibility.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    resumed.snapshot('guest', [slot('table', 'Fresh after unlock')])
    resumed.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Fresh after unlock')
  })

  it('recovers a UTF-8 frame split across chunks and CRLFs and reopens after planned reconnect', async () => {
    const initial = streamFixture()
    const retry = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(initial.response).mockResolvedValueOnce(retry.response)
    await start(vi.fn(), undefined, open)
    initial.frame('session', { role: 'guest', audio: false })
    const json = JSON.stringify({ ...event, event: 'snapshot', slots: [slot('table', 'A dragon 🐉')] })
    const encoded = new TextEncoder().encode(`event: snapshot\r\ndata: ${json}\r\n\r\n`)
    const dragon = encoded.indexOf(0xf0) + 1
    initial.chunk(encoded.slice(0, dragon))
    initial.chunk(encoded.slice(dragon, -1))
    initial.chunk(encoded.slice(-1))
    initial.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('A dragon 🐉')
    initial.frame('reconnect')
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).toBeEmptyDOMElement()
    expect(open).toHaveBeenCalledTimes(2)
  })

  it('announces snapshot fallback and never shows a private frame on a guest stream', async () => {
    const live = streamFixture()
    await start(async () => picture('guest', slot('table', 'Fallback handout')), undefined, async () => live.response)
    live.snapshot('guest', [slot('table', 'Authorized'), slot('mine', 'Forbidden')])
    live.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Fallback handout')
    expect(content()).not.toHaveTextContent('Forbidden')
    expect(connection()).toHaveTextContent('Live updates are delayed')
  })

  it('reports stream capacity with Retry-After and retries admission before fetching a snapshot', async () => {
    const retry = streamFixture()
    const request = vi.fn()
    const open = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 503, headers: { 'Retry-After': '2' } }))
      .mockResolvedValueOnce(retry.response)
    await start(request, undefined, open)
    expect(status()).toBe('This table is full.')
    await vi.advanceTimersByTimeAsync(1999)
    expect(open).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(open).toHaveBeenCalledTimes(2)
    expect(request).not.toHaveBeenCalled()
    retry.snapshot('guest', [slot('table', 'Now admitted')])
    retry.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Now admitted')
  })
})
