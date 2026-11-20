import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import html from '../../table.html?raw'
import { tableAddress, tableCampaign } from './address'
import { mountTable } from './table'
import type { IdentityChannelLike } from '../shell/identityBroadcast'

const event = { schema_version: 1 }
const slot = (name: 'table' | 'mine', title: string | null) => ({
  slot: name, seq: 1, content: title === null ? null : {
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

function start(request: typeof fetch, channel?: IdentityChannelLike) {
  stop = mountTable(document, 'cmp_table', request, () => channel ?? null)
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
