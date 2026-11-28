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

const identityOf = (kind: 'account' | 'screen') => Response.json({ ...event, kind })
const noIdentity = () => Response.json({ detail: 'not signed in' }, { status: 401 })

function start(request: typeof fetch, channel?: IdentityChannelLike, stream?: typeof fetch,
  identify: typeof fetch = async () => identityOf('account'), campaign: string | null = 'cmp_table') {
  stop = mountTable(document, campaign, (input, init) => {
    if (input === '/table/identity') return identify(input, init)
    if (String(input).startsWith('/table/stream?')) return stream?.(input, init) ?? Promise.resolve(new Response(null, { status: 404 }))
    return request(input, init)
  }, () => channel ?? null)
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
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('account'))
      .mockImplementation(async () => code === 401 ? noIdentity() : identityOf('account'))
    await start(request, undefined, undefined, identify)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toBe(code === 401 ? 'Sign in to open this table.' : 'This table is unavailable.')
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
    expect(status()).toBe('This table is unavailable.')
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
    expect(request).toHaveBeenLastCalledWith('/table/sign-out', {
      method: 'POST', cache: 'no-store', redirect: 'error', signal: expect.any(AbortSignal),
    })
    expect(channel.postMessage).toHaveBeenCalledWith({ v: 1, kind: 'identity-changed' })
    expect(document.querySelector('#table-sign-in')).toHaveFocus()
  })
})

const exit = (id: 'sign-in' | 'sign-out' | 'leave' | 'reload') =>
  document.querySelector<HTMLElement>(`#table-${id}`)!

describe('effective account or screen credential', () => {
  it.each([
    ['account', 'guest'], ['account', 'participant'], ['screen', 'guest'],
  ] as const)('shows the %s exit regardless of a %s authorization picture', async (actor, role) => {
    const slots = [slot('table', 'Handout'), ...(role === 'participant' ? [slot('mine', null)] : [])]
    await start(async () => picture(role, ...slots), undefined, undefined, async () => identityOf(actor))
    expect(content()).toHaveTextContent('Handout')
    expect(exit('sign-out').hidden).toBe(actor !== 'account')
    expect(exit('leave').hidden).toBe(actor !== 'screen')
    expect(exit('sign-in')).not.toBeVisible()
    expect(document.querySelector('#table-return')).toHaveTextContent(actor === 'account' ? 'Back to your seats' : 'Back to the Tavern')
  })

  it.each(['account', 'screen'] as const)('keeps the %s exit on a cold full table or missing address', async actor => {
    const identify = vi.fn(async () => identityOf(actor))
    const requests = vi.fn<typeof fetch>()
    const full = vi.fn(async () => new Response(null, { status: 503, headers: { 'Retry-After': '5' } }))
    await start(requests, undefined, full, identify, null)
    expect(status()).toBe('This table is unavailable.')
    expect(identify).toHaveBeenCalledOnce()
    expect(full).not.toHaveBeenCalled()
    expect(exit(actor === 'account' ? 'sign-out' : 'leave')).toBeVisible()
    stop!()
    await start(requests, undefined, full, identify)
    expect(status()).toBe('This table is full. Ask your GM to make room.')
    expect(exit(actor === 'account' ? 'sign-out' : 'leave')).toBeVisible()
    expect(requests).not.toHaveBeenCalled()
  })

  it.each([
    () => new Response(null, { status: 404 }),
    () => new Response('<html>Earlier application</html>'),
    () => Response.json({ kind: 'participant', ...event }),
    () => Response.json({ detail: 'unknown proxy reply' }, { status: 401 }),
    () => { throw new TypeError('offline') },
  ])('keeps authorized content while a delayed or incompatible identity read can offer no destructive exit', async refuse => {
    let complete!: () => void
    const identify = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise((resolve, reject) => {
      complete = () => { try { resolve(refuse()) } catch (error) { reject(error) } }
    })).mockImplementation(async () => identityOf('account'))
    const request = vi.fn(async () => picture('guest', slot('table', 'A real public handout')))
    await start(request, undefined, undefined, identify)
    expect(content()).toHaveTextContent('A real public handout')
    expect(exit('sign-out')).not.toBeVisible()
    expect(exit('leave')).not.toBeVisible()
    complete()
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('reload')).toHaveTextContent('Retry')
    expect(status()).not.toMatch(/signed in|check/i)
    expect(content()).toHaveTextContent('A real public handout')
    exit('sign-out').click()
    exit('leave').click()
    expect(identify).toHaveBeenCalledOnce()
    expect(request).toHaveBeenCalledOnce()
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('sign-out')).toBeVisible()
    expect(request).toHaveBeenCalledTimes(2)
    expect(content()).toHaveTextContent('A real public handout')
  })

  it('respects identity Retry-After through manual reload and visibility changes without delaying fresh table data', async () => {
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, {
      status: 429, headers: { 'Retry-After': '15' },
    })).mockImplementation(async () => identityOf('screen'))
    const request = vi.fn(async () => picture('guest', slot('table', 'Live handout')))
    await start(request, undefined, undefined, identify)
    expect(exit('reload')).toHaveAttribute('aria-disabled', 'true')
    exit('reload').click()
    document.dispatchEvent(new Event('freeze'))
    document.dispatchEvent(new Event('resume'))
    await vi.advanceTimersByTimeAsync(14_999)
    expect(identify).toHaveBeenCalledOnce()
    expect(content()).toHaveTextContent('Live handout')
    await vi.advanceTimersByTimeAsync(1)
    expect(identify).toHaveBeenCalledTimes(2)
    expect(exit('leave')).toBeVisible()
    expect(exit('reload')).not.toHaveAttribute('aria-disabled')
  })

  it('keeps an oversized identity Retry-After from overflowing the browser timer', async () => {
    const identify = vi.fn(async () => new Response(null, { status: 429, headers: { 'Retry-After': '9'.repeat(400) } }))
    await start(vi.fn(), undefined, undefined, identify, null)
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(300_000)
    expect(identify).toHaveBeenCalledOnce()
    expect(exit('reload')).toHaveAttribute('aria-disabled', 'true')
  })

  it('a screen Leave preflight reopens the account table when the screen grant has gone', async () => {
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('screen'))
      .mockImplementation(async () => identityOf('account'))
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('guest', slot('table', 'Screen handout')))
      .mockImplementation(async () => picture('participant', slot('table', 'Public'), slot('mine', 'Current private note')))
    await start(request, undefined, undefined, identify)
    exit('leave').focus()
    exit('leave').click()
    expect(content()).toBeEmptyDOMElement()
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('sign-out')).toBeVisible()
    expect(exit('leave')).not.toBeVisible()
    expect(document.querySelector('h1')).toHaveFocus()
    expect(content()).toHaveTextContent('Current private note')
    expect(content()).not.toHaveTextContent('Screen handout')
    expect(request.mock.calls.map(([path]) => path)).toEqual(Array(2).fill('/table/snapshot?campaign_id=cmp_table'))
  })

  it('an observed screen revocation stays neutral until an explicit identity change', async () => {
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('screen')).mockImplementation(async () => noIdentity())
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('guest', slot('table', 'Screen handout')))
      .mockImplementation(async () => new Response(null, { status: 401 }))
    await start(request, channel, undefined, identify)
    expect(exit('leave')).toBeVisible()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(status()).toBe('This table is unavailable.')
    expect(exit('leave')).not.toBeVisible()
    expect(exit('sign-in')).not.toBeVisible()
    expect(content()).toBeEmptyDOMElement()
    channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('sign-in')).toBeVisible()
  })

  it('a screen winning at POST time refreshes the page without a second mutation or a sign-out notice', async () => {
    let finishIdentity!: (response: Response) => void
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('account'))
      .mockImplementationOnce(() => new Promise(resolve => { finishIdentity = resolve }))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', 'Private note')))
      .mockResolvedValueOnce(new Response(null, { status: 409 }))
      .mockResolvedValueOnce(picture('guest', slot('table', 'Public handout')))
    await start(request, channel, undefined, identify)
    exit('sign-out').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    expect(exit('sign-out')).not.toBeVisible()
    expect(exit('leave')).not.toBeVisible()
    finishIdentity(identityOf('screen'))
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('leave')).toBeVisible()
    expect(content()).toHaveTextContent('Public handout')
    expect(status()).not.toMatch(/signed out/i)
    expect(channel.postMessage).not.toHaveBeenCalled()
    expect(request.mock.calls.filter(([, init]) => init?.method === 'POST').map(([path]) => path)).toEqual(['/table/sign-out'])
  })

  it.each(['account', 'screen'] as const)('keeps the %s exit available after an old shell or proxy answers the POST', async actor => {
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const request = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('<html>Old route</html>'))
      .mockResolvedValueOnce(new Response('<html>Sign in to proxy</html>', { status: 401 }))
      .mockResolvedValueOnce(actor === 'account' ? noIdentity() : new Response(null, { status: 204 }))
    await start(request, channel, undefined, async () => identityOf(actor), null)
    const action = exit(actor === 'account' ? 'sign-out' : 'leave')
    action.focus()
    for (let attempt = 0; attempt < 2; attempt++) {
      action.click()
      await vi.advanceTimersByTimeAsync(0)
      expect(status()).toContain('Try again')
      expect(action).toHaveFocus()
      expect(exit('sign-in')).not.toBeVisible()
      expect(channel.postMessage).not.toHaveBeenCalled()
    }
    action.click()
    await vi.advanceTimersByTimeAsync(0)
    expect(status()).toBe(actor === 'account' ? 'You signed out.' : 'You left the table.')
    expect(channel.postMessage).toHaveBeenCalledTimes(actor === 'account' ? 1 : 0)
    expect(request.mock.calls.every(([, init]) => init?.redirect === 'error')).toBe(true)
  })

  it('discards a slow identity response superseded by an explicit cross-tab switch', async () => {
    let first!: (response: Response) => void
    const identify = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { first = resolve }))
      .mockImplementation(async () => identityOf('screen'))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', 'Old note')))
      .mockImplementation(async () => picture('guest', slot('table', 'Current screen handout')))
    await start(request, channel, undefined, identify)
    expect(content()).toHaveTextContent('Old note')
    channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    expect(content()).toBeEmptyDOMElement()
    await vi.advanceTimersByTimeAsync(0)
    first(identityOf('account'))
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('leave')).toBeVisible()
    expect(exit('sign-out')).not.toBeVisible()
    expect(content()).toHaveTextContent('Current screen handout')
    expect(content()).not.toHaveTextContent('Old note')
  })

  it.each(['screen', 'account'] as const)('reopens an unverified live stream and its audio registration for the first verified %s', async actor => {
    const provisional = streamFixture()
    provisional.response.headers.set('X-Table-Audio-Context', 'c'.repeat(64))
    const fresh = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(provisional.response).mockResolvedValueOnce(fresh.response)
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockImplementation(async () => identityOf(actor))
    const request = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const body = JSON.parse(String(init?.body))
      return body.action === 'open' ? Response.json({ ...event, registration: 'r'.repeat(32) }) : new Response(null, { status: 204 })
    })
    await start(request, undefined, open, identify)
    provisional.frame('session', { role: 'participant', audio: true })
    provisional.frame('snapshot', { slots: [slot('table', null), slot('mine', 'Previous private note')] })
    provisional.frame('ready')
    await vi.advanceTimersByTimeAsync(3)
    expect(content()).toHaveTextContent('Previous private note')
    expect(document.querySelector('#table-sound section')).toBeVisible()
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(provisional.cancel).toHaveBeenCalledOnce()
    expect(content()).toBeEmptyDOMElement()
    expect(document.querySelector('#table-sound section')).not.toBeVisible()
    expect(request.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))).toContainEqual({
      ...event, action: 'close', registration: 'r'.repeat(32),
    })
    expect(exit(actor === 'screen' ? 'leave' : 'sign-out')).toBeVisible()
    fresh.snapshot('guest', [slot('table', 'Fresh public view')])
    fresh.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Fresh public view')
    expect(content()).not.toHaveTextContent('Previous private note')
  })

  it('discards a delayed provisional private snapshot that arrives after the verified screen view', async () => {
    let oldPicture!: (response: Response) => void
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { oldPicture = resolve }))
      .mockImplementation(async () => picture('guest', slot('table', 'Fresh public view')))
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockImplementation(async () => identityOf('screen'))
    await start(request, undefined, undefined, identify)
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(exit('leave')).toBeVisible()
    expect(content()).toHaveTextContent('Fresh public view')
    expect(request.mock.calls[0][1]?.signal?.aborted).toBe(true)
    oldPicture(picture('participant', slot('table', 'Previous public note'), slot('mine', 'Previous private note')))
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('Fresh public view')
    expect(content()).not.toHaveTextContent('Previous')
  })

  it.each(['broadcast', 'freeze', 'dispose'])('a late first-account recheck cannot restore private data after %s', async interrupt => {
    let late!: (response: Response) => void
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('guest', slot('table', 'Provisional')))
      .mockImplementationOnce(() => new Promise(resolve => { late = resolve }))
      .mockImplementation(async () => picture('guest', slot('table', 'Current screen')))
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(identityOf('account')).mockImplementation(async () => identityOf('screen'))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    await start(request, channel, undefined, identify)
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toBeEmptyDOMElement()
    if (interrupt === 'broadcast') channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    else if (interrupt === 'freeze') document.dispatchEvent(new Event('freeze'))
    else { stop!(); stop = undefined }
    expect(request.mock.calls[1][1]?.signal?.aborted).toBe(true)
    late(picture('participant', slot('table', null), slot('mine', 'Private previous view')))
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).not.toHaveTextContent('Private previous view')
    if (interrupt === 'broadcast') expect(content()).toHaveTextContent('Current screen')
    else expect(content()).toBeEmptyDOMElement()
  })

  it('blocks contradictory participant data on a verified screen and retries at a bounded rate through an identity outage', async () => {
    const wrong = () => picture('participant', slot('table', 'Wrong public scope'), slot('mine', 'Private note'))
    const request = vi.fn<typeof fetch>().mockImplementationOnce(async () => wrong()).mockImplementationOnce(async () => wrong())
      .mockImplementation(async () => picture('guest', slot('table', 'Fresh public view')))
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('screen'))
      .mockResolvedValueOnce(new Response(null, { status: 429, headers: { 'Retry-After': '30' } }))
      .mockImplementation(async () => identityOf('screen'))
    await start(request, undefined, undefined, identify)
    expect(content()).toBeEmptyDOMElement()
    expect(exit('leave')).not.toBeVisible()
    await vi.advanceTimersByTimeAsync(4999)
    expect(request).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(5000)
    expect(request).toHaveBeenCalledTimes(2)
    expect(identify).toHaveBeenCalledTimes(2)
    expect(content()).toBeEmptyDOMElement()
    await vi.advanceTimersByTimeAsync(1)
    expect(content()).toHaveTextContent('Fresh public view')
    expect(exit('sign-out')).not.toBeVisible()
  })

  it('a verified account can sign out even if the next credential read would be throttled', async () => {
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('account'))
      .mockImplementation(async () => new Response(null, { status: 429 }))
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 204 }))
    await start(request, undefined, undefined, identify, null)
    exit('sign-out').click()
    await vi.advanceTimersByTimeAsync(0)
    expect(request.mock.calls[0][0]).toBe('/table/sign-out')
    expect(identify).toHaveBeenCalledOnce()
    expect(status()).toBe('You signed out.')
  })

  it.each([
    ['account', 'accepted', true], ['account', 'already signed out', true],
    ['account', 'unrecognized 401', false], ['screen', 'accepted', false],
  ] as const)('clears account storage on an explicit %s exit only when confirmed (%s)', async (actor, outcome, cleared) => {
    const key = 'game-guide-ai:conversations:shared-device'
    localStorage.setItem(key, 'Private conversation title')
    try {
      const reply = outcome === 'accepted' ? new Response(null, { status: 204 })
        : outcome === 'already signed out' ? noIdentity() : new Response('proxy denied', { status: 401 })
      await start(async () => reply, undefined, undefined, async () => identityOf(actor), null)
      exit(actor === 'account' ? 'sign-out' : 'leave').click()
      await vi.advanceTimersByTimeAsync(0)
      expect(localStorage.getItem(key)).toBe(cleared ? null : 'Private conversation title')
    } finally { localStorage.removeItem(key) }
  })

  it.each(['account', 'screen'] as const)('returns focusable retry to the %s exit when its POST stalls', async actor => {
    let late!: (response: Response) => void
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('guest', slot('table', 'Live handout')))
      .mockImplementationOnce(() => new Promise(resolve => { late = resolve }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    await start(request, channel, undefined, async () => identityOf(actor))
    const action = exit(actor === 'screen' ? 'leave' : 'sign-out')
    action.focus()
    action.click()
    await vi.advanceTimersByTimeAsync(7999)
    expect(action).toHaveAttribute('aria-disabled', 'true')
    await vi.advanceTimersByTimeAsync(1)
    expect(action).not.toHaveAttribute('aria-disabled')
    expect(action).toHaveFocus()
    expect(request.mock.calls[1][1]?.signal?.aborted).toBe(true)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toContain('Try again')
    late(new Response(null, { status: 204 }))
    await vi.advanceTimersByTimeAsync(0)
    expect(channel.postMessage).not.toHaveBeenCalled()
    expect(status()).toContain('Try again')
    action.click()
    await vi.advanceTimersByTimeAsync(0)
    expect(status()).toBe(actor === 'screen' ? 'You left the table.' : 'You signed out.')
  })

  it.each(['pagehide', 'dispose'])('a stalled exit interrupted by %s never announces a timeout', async interrupt => {
    const request = vi.fn<typeof fetch>().mockImplementation(() => new Promise(() => {}))
    await start(request, undefined, undefined, async () => identityOf('account'), null)
    exit('sign-out').click()
    if (interrupt === 'pagehide') window.dispatchEvent(new Event('pagehide'))
    else { stop!(); stop = undefined }
    await vi.advanceTimersByTimeAsync(9000)
    expect(request.mock.calls[0][1]?.signal?.aborted).toBe(true)
    expect(status()).not.toMatch(/Try again|signed out/)
  })

  it.each(['broadcast', 'freeze', 'dispose'])('discards a pending screen Leave preflight after %s', async transition => {
    let answer!: (response: Response) => void
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('screen'))
      .mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
      .mockImplementation(async () => identityOf('account'))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    const request = vi.fn<typeof fetch>().mockImplementation(async () => picture('guest', slot('table', 'Current table')))
    await start(request, channel, undefined, identify)
    exit('leave').click()
    if (transition === 'broadcast') channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    else if (transition === 'freeze') {
      document.dispatchEvent(new Event('freeze'))
      document.dispatchEvent(new Event('resume'))
    } else {
      stop!()
      stop = undefined
    }
    answer(identityOf('screen'))
    await vi.advanceTimersByTimeAsync(0)
    expect(channel.postMessage).not.toHaveBeenCalled()
    expect(request.mock.calls.every(args => String(args[0]).startsWith('/table/snapshot?'))).toBe(true)
    if (transition !== 'dispose') expect(exit('sign-out')).toBeVisible()
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

const limitedStatus = 'Too many requests. Wait a moment and try again.'
const throttled = (seconds = '25') => Response.json({ private: 'Refusal details' }, { status: 429, headers: { 'Retry-After': seconds } })
const noStream = () => new Response(null, { status: 404 })

describe('table-read throttling', () => {
  it.each(['stream', 'snapshot'])('a %s refusal clears the live view and holds every read until its deadline', async transport => {
    const next = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(noStream())
      .mockResolvedValueOnce(transport === 'stream' ? throttled() : noStream()).mockResolvedValueOnce(next.response)
    const snapshot = vi.fn<typeof fetch>().mockResolvedValueOnce(picture('participant', slot('table', null), slot('mine', 'Private note')))
      .mockImplementation(async () => throttled())
    const identify = vi.fn<typeof fetch>().mockImplementation(async () => identityOf('account'))
    await start(snapshot, undefined, open, identify)
    expect(content()).toHaveTextContent('Private note')
    await vi.advanceTimersByTimeAsync(5000)
    expect(content()).toBeEmptyDOMElement()
    expect(status()).toBe(limitedStatus)
    expect(connection()).toBeEmptyDOMElement()
    expect(exit('reload')).toHaveAttribute('aria-disabled', 'true')
    const refused = transport === 'stream' ? open : snapshot
    expect(refused.mock.calls.at(-1)?.[1]?.signal?.aborted).toBe(true)

    exit('reload').click()
    visibility.mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(12_000)
    window.dispatchEvent(new Event('pagehide'))
    visibility.mockReturnValue('visible')
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))
    document.dispatchEvent(new Event('visibilitychange'))
    document.dispatchEvent(new Event('freeze'))
    document.dispatchEvent(new Event('resume'))
    await vi.advanceTimersByTimeAsync(12_999)
    expect(status()).toBe(limitedStatus)
    expect(document.body).not.toHaveTextContent(/Private note|Refusal details/)
    expect(open).toHaveBeenCalledTimes(2)
    expect(snapshot).toHaveBeenCalledTimes(transport === 'stream' ? 1 : 2)
    expect(identify).toHaveBeenCalledOnce()

    await vi.advanceTimersByTimeAsync(2)
    expect(identify).toHaveBeenCalledTimes(2)
    expect(open).toHaveBeenCalledTimes(3)
    next.snapshot('guest', [slot('table', 'New authorized handout')])
    next.frame('ready')
    await vi.advanceTimersByTimeAsync(0)
    expect(content()).toHaveTextContent('New authorized handout')
    expect(exit('reload')).not.toHaveAttribute('aria-disabled')
  })

  it('ignores a stale identity answer and holds an explicit cross-tab credential change through backoff and disposal', async () => {
    let late!: (response: Response) => void
    const identify = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { late = resolve }))
      .mockImplementation(async () => identityOf('account'))
    const open = vi.fn<typeof fetch>().mockImplementation(async () => throttled())
    const snapshot = vi.fn<typeof fetch>()
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() } as IdentityChannelLike
    await start(snapshot, channel, open, identify)
    expect(identify.mock.calls[0][1]?.signal?.aborted).toBe(true)
    late(identityOf('account'))
    channel.onmessage?.(new MessageEvent('message', { data: { v: 1, kind: 'identity-changed' } }))
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(24_999)
    expect(status()).toBe(limitedStatus)
    expect(exit('sign-out')).not.toBeVisible()
    expect(exit('leave')).not.toBeVisible()
    stop!()
    stop = undefined
    await vi.advanceTimersByTimeAsync(30_000)
    expect(identify).toHaveBeenCalledOnce()
    expect(open).toHaveBeenCalledOnce()
    expect(snapshot).not.toHaveBeenCalled()
  })

  it.each(['account', 'screen'] as const)('a known %s can explicitly exit during backoff without any read', async actor => {
    const identify = vi.fn<typeof fetch>().mockImplementation(async () => identityOf(actor))
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled())
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 204 }))
    await start(request, undefined, open, identify)
    expect(status()).toBe(limitedStatus)
    exit(actor === 'account' ? 'sign-out' : 'leave').click()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(identify).toHaveBeenCalledOnce()
    expect(open).toHaveBeenCalledOnce()
    expect(request.mock.calls.map(([path, init]) => [path, init?.method])).toEqual([[actor === 'account' ? '/table/sign-out' : '/table/leave', 'POST']])
    expect(status()).toBe(actor === 'account' ? 'You signed out.' : 'You left the table.')
  })

  it.each([3, 40])('a refused Leave honors its own %is deadline while the handout stays redacted', async seconds => {
    const identify = vi.fn<typeof fetch>().mockImplementation(async () => identityOf('screen'))
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled())
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled(String(seconds)))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() }
    await start(request, channel, open, identify)
    const leave = exit('leave')
    leave.focus()
    leave.click()
    await vi.advanceTimersByTimeAsync(0)
    expect(status()).toBe('Couldn’t leave the table. Wait a moment and try again.')
    expect(leave).toHaveAttribute('aria-disabled', 'true')
    expect(leave).toHaveFocus()
    leave.click()
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(seconds * 1000 - 1)
    expect(leave).toHaveAttribute('aria-disabled', 'true')
    expect(exit('reload')).toHaveAttribute('aria-disabled', 'true')
    expect(channel.postMessage).not.toHaveBeenCalled()
    expect(request).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(leave).not.toHaveAttribute('aria-disabled')
    expect(exit('reload').getAttribute('aria-disabled')).toBe(seconds < 25 ? 'true' : null)
    expect(status()).toContain('Couldn’t leave')
    expect(content()).toBeEmptyDOMElement()
    expect(open).toHaveBeenCalledTimes(1)
    expect(identify).toHaveBeenCalledTimes(1)
    leave.click()
    await vi.advanceTimersByTimeAsync(0)
    expect(request.mock.calls.map(([path]) => path)).toEqual(['/table/leave', '/table/leave'])
    expect(status()).toBe('You left the table.')
  })

  it.each(['failure', 'timeout'])('re-enables Reload when the read budget recovers during sign-out %s', async failure => {
    const identify = vi.fn<typeof fetch>().mockImplementation(async () => identityOf('account'))
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled('2')).mockResolvedValueOnce(noStream())
    let answer!: (response: Response) => void
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
      .mockResolvedValueOnce(picture('guest', slot('table', 'Fresh handout')))
    const channel = { onmessage: null, postMessage: vi.fn(), close: vi.fn() }
    await start(request, channel, open, identify)
    const action = exit('sign-out')
    action.focus()
    action.click()
    await vi.advanceTimersByTimeAsync(2000)
    expect(exit('reload')).not.toHaveAttribute('aria-disabled')
    expect(action).toHaveAttribute('aria-disabled', 'true')
    if (failure === 'failure') answer(new Response(null, { status: 503 }))
    await vi.advanceTimersByTimeAsync(12_000)
    expect(action).not.toHaveAttribute('aria-disabled')
    expect(action).toHaveFocus()
    expect(status()).toBe('Couldn’t sign out. Try again.')
    expect(channel.postMessage).not.toHaveBeenCalled()
    expect(content()).toBeEmptyDOMElement()
    expect(open).toHaveBeenCalledTimes(1)
    expect(identify).toHaveBeenCalledTimes(1)
    exit('reload').click()
    await vi.advanceTimersByTimeAsync(2)
    expect(content()).toHaveTextContent('Fresh handout')
  })

  it('keeps a deadline beyond the maximum timer interval without early reads', async () => {
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled('3000000'))
    const identify = vi.fn<typeof fetch>().mockImplementation(async () => identityOf('account'))
    const request = vi.fn<typeof fetch>()
    await start(request, undefined, open, identify)
    await vi.advanceTimersByTimeAsync(2_147_483_649)
    exit('reload').click()
    expect(status()).toBe(limitedStatus)
    expect(exit('reload')).toHaveAttribute('aria-disabled', 'true')
    expect(open).toHaveBeenCalledOnce()
    expect(identify).toHaveBeenCalledOnce()
    expect(request).not.toHaveBeenCalled()
  })

  it('a guarded sign-out conflict during backoff holds both the new actor and the view for fresh authorization', async () => {
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('account')).mockResolvedValueOnce(identityOf('screen'))
    const next = streamFixture()
    const open = vi.fn<typeof fetch>().mockResolvedValueOnce(throttled()).mockResolvedValueOnce(next.response)
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(null, { status: 409 }))
    await start(request, undefined, open, identify)
    exit('sign-out').click()
    await vi.advanceTimersByTimeAsync(24_999)
    expect(identify).toHaveBeenCalledOnce()
    expect(open).toHaveBeenCalledOnce()
    expect(exit('sign-out')).not.toBeVisible()
    expect(exit('leave')).not.toBeVisible()
    expect(status()).toBe(limitedStatus)
    await vi.advanceTimersByTimeAsync(2)
    expect(exit('leave')).toBeVisible()
    expect(open).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenCalledOnce()
  })
})

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
      if (transition === 'inactive') expect(status()).toBe('This table is unavailable.')
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
    const identify = vi.fn<typeof fetch>().mockResolvedValueOnce(identityOf('account')).mockImplementation(async () => noIdentity())
    await start(request, undefined, undefined, identify)
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
    await vi.advanceTimersByTimeAsync(0)
    finishSignOut(new Response(null, { status: 204 }))
    await vi.advanceTimersByTimeAsync(0)
    expect(request.mock.calls.filter(([url]) => url === '/table/sign-out')).toHaveLength(1)
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
      expect(status()).toBe('This table is unavailable.')
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
    expect(status()).toBe('This table is unavailable.')
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
    expect(status()).toBe('This table is full. Ask your GM to make room.')
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
