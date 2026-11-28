import { CONTRACT_VERSION } from '../gm/contracts'
import { openIdentityBroadcast, type IdentityChannelFactory } from '../shell/identityBroadcast'
import { renderProjections } from './projection'
import { mountTableAudio } from './audioControls'
import type { MixerCue } from './audioMixer'
import { tablePresence } from './presence'
import { responseClock, streamTable, tablePicture, type TablePicture } from './stream'

const POLL_MS = 5000
const BLANK_MS = 10_000
const READ_TIMEOUT_MS = 8000

type ReadResult = TablePicture | { kind: 'inactive' | 'signed-out' | 'retry' } | { kind: 'limited'; delay: number }

async function readTable(id: string, signal: AbortSignal, request: typeof fetch): Promise<ReadResult> {
  try {
    const sentAt = Date.now()
    const response = await request(`/table/snapshot?campaign_id=${encodeURIComponent(id)}`, { cache: 'no-store', signal })
    if (response.status === 401) return { kind: 'signed-out' }
    if (response.status === 404) return { kind: 'inactive' }
    if (response.status === 429) {
      const seconds = Number(response.headers.get('Retry-After'))
      return { kind: 'limited', delay: Number.isFinite(seconds) ? Math.max(POLL_MS, Math.min(seconds, 300) * 1000) : POLL_MS }
    }
    if (!response.ok) return { kind: 'retry' }
    const offset = responseClock(response, sentAt)
    return tablePicture(await response.json(), offset, response.headers.get('X-Table-Audio-Context'))
  } catch {
    return { kind: 'retry' }
  }
}

const COPY = {
  joining: 'Joining the table…',
  waiting: 'Nothing to see yet. Your GM hasn’t shared anything.',
  changed: 'Your GM shared something new.',
  reconnecting: 'Reconnecting…',
  degraded: 'Reconnecting… Live updates are delayed.',
  hidden: 'Reconnecting… hidden until the connection is back.',
  inactive: 'This table isn’t active or available to you.',
  signedOut: 'Sign in to open this table.',
  limited: 'Too many requests. Wait a moment and try again.',
  full: 'This table is full.',
}

export function mountTable(
  document: Document,
  campaignId: string | null,
  request: typeof fetch = fetch,
  channelFactory?: IdentityChannelFactory,
): () => void {
  const status = document.querySelector<HTMLElement>('#table-status')!
  const connection = document.querySelector<HTMLElement>('#table-connection')!
  const projections = document.querySelector<HTMLElement>('#table-projections')!
  const reload = document.querySelector<HTMLButtonElement>('#table-reload')!
  const signIn = document.querySelector<HTMLAnchorElement>('#table-sign-in')!
  const signOut = document.querySelector<HTMLButtonElement>('#table-sign-out')!
  const leave = document.querySelector<HTMLButtonElement>('#table-leave')!
  const browser = document.defaultView!
  let clockOffsetMs = 0
  const presence = tablePresence(campaignId, request, recheck)
  const audio = mountTableAudio(document.querySelector<HTMLElement>('#table-sound')!, presence.update,
    () => Date.now() + clockOffsetMs)

  let activeRead: AbortController | null = null
  let pollTimer: number | undefined
  let blankTimer: number | undefined
  let soundTimer: number | undefined
  let resync = true
  let pictureKey: string | null = null
  let streamRetryAt = 0
  let streamFailures = 0
  let paused = false
  let departed = false
  let actionPending = false

  function announce(message: string): void {
    if (status.textContent !== message) status.textContent = message
  }

  function showDegraded(degraded: boolean): void {
    const message = degraded ? COPY.degraded : ''
    if (connection.textContent !== message) connection.textContent = message
  }

  function redact(): void {
    projections.replaceChildren()
    pictureKey = null
  }

  function reset(): void {
    browser.clearTimeout(pollTimer)
    browser.clearTimeout(blankTimer)
    activeRead?.abort()
    activeRead = null
    streamRetryAt = 0
    streamFailures = 0
    showDegraded(false)
    redact()
  }

  function queue(delay: number): void {
    pollTimer = browser.setTimeout(() => { void read() }, delay)
  }

  function showPicture(result: TablePicture): void {
    clockOffsetMs = result.clockOffsetMs
    const slots: { ambience: MixerCue | null; one_shot: MixerCue | null } = { ambience: null, one_shot: null }
    for (const frame of result.audio) {
      const playing = frame.playing
      if (playing) slots[frame.slot] = {
        url: `/table/assets/${encodeURIComponent(playing.asset.handle)}`, seq: frame.seq,
        started_at: playing.started_at, start_offset_ms: playing.start_offset_ms,
        duration_ms: playing.duration_ms, loop: playing.loop,
      }
    }
    audio.show(result.audioEnabled && result.audioContext !== null, slots, resync)
    presence.authorize(result.audioEnabled ? result.audioContext : null)
    resync = false
    if (document.visibilityState === 'hidden') return
    signOut.hidden = !result.participant
    leave.hidden = result.participant
    const key = JSON.stringify(result.slots.map(({ slot, content }) => [slot, content]))
    if (key !== pictureKey) {
      const wasLive = pictureKey !== null
      const showing = renderProjections(projections, result.slots)
      announce(showing ? (wasLive ? COPY.changed : '') : COPY.waiting)
      pictureKey = key
    } else if (status.textContent === COPY.reconnecting) {
      announce(projections.childElementCount > 0 ? '' : COPY.waiting)
    }
  }

  function armSilenceTimer(onSilence?: () => void): void {
    browser.clearTimeout(blankTimer)
    blankTimer = browser.setTimeout(() => {
      redact()
      announce(COPY.hidden)
      onSilence?.()
    }, BLANK_MS)
  }

  function hearFromTable(): void {
    browser.clearTimeout(soundTimer)
    soundTimer = browser.setTimeout(() => {
      audio.silence()
      resync = true
    }, BLANK_MS)
  }

  function clearSound(): void {
    browser.clearTimeout(soundTimer)
    audio.hide()
    presence.reset()
    resync = true
  }

  function finish(kind: 'signed-out' | 'inactive'): void {
    browser.clearTimeout(blankTimer)
    redact()
    clearSound()
    showDegraded(false)
    signIn.hidden = kind !== 'signed-out'
    if (!signIn.hidden) {
      signOut.hidden = true
      leave.hidden = true
    }
    announce(kind === 'signed-out' ? COPY.signedOut : COPY.inactive)
  }

  async function openStream(id: string, controller: AbortController): Promise<void> {
    let opened = false
    const deadline = browser.setTimeout(() => controller.abort(), READ_TIMEOUT_MS)
    let ready = false
    const watchBytes = () => {
      armSilenceTimer(() => controller.abort())
      if (ready) hearFromTable()
    }
    const end = await streamTable(id, controller.signal, request, {
      hidden: () => document.visibilityState === 'hidden',
      opened() {
        opened = true
        resync = true
        redact()
        watchBytes()
      },
      bytes: watchBytes,
      picture(picture) {
        ready = true
        browser.clearTimeout(deadline)
        hearFromTable()
        streamFailures = 0
        showDegraded(false)
        showPicture(picture)
      },
    })
    browser.clearTimeout(deadline)
    if (activeRead !== controller) return
    activeRead = null
    if (opened) redact()
    if (end.kind === 'signed-out' || end.kind === 'inactive') {
      finish(end.kind)
    } else if (end.kind === 'reconnect') {
      queue(0)
    } else if (end.kind === 'full') {
      redact()
      clearSound()
      showDegraded(false)
      announce(COPY.full)
      queue(end.delay)
    } else {
      const backoff = Math.min(30_000, 1000 * 2 ** Math.min(streamFailures, 5))
      streamFailures += 1
      streamRetryAt = Date.now() + Math.min(30_000, backoff * (0.75 + Math.random() * 0.5))
      showDegraded(true)
      if (opened) announce(COPY.hidden)
      void read()
    }
  }

  async function read(): Promise<void> {
    if (campaignId === null || paused || departed || actionPending || activeRead !== null) return
    const controller = new AbortController()
    activeRead = controller
    if (Date.now() >= streamRetryAt) {
      await openStream(campaignId, controller)
      return
    }
    const deadline = browser.setTimeout(() => controller.abort(), READ_TIMEOUT_MS)
    const result = await readTable(campaignId, controller.signal, request)
    browser.clearTimeout(deadline)
    if (activeRead !== controller) return
    activeRead = null
    if (result.kind === 'live') {
      hearFromTable()
      showPicture(result)
      armSilenceTimer()
      queue(POLL_MS)
      return
    }
    if (result.kind === 'retry') {
      announce(projections.childElementCount > 0 ? COPY.reconnecting : COPY.hidden)
      queue(POLL_MS)
      return
    }
    redact()
    if (result.kind === 'limited') {
      announce(COPY.limited)
      queue(result.delay)
      return
    }
    finish(result.kind)
  }

  function recheck(): void {
    if (departed || actionPending) return
    reset()
    resync = true
    signIn.hidden = true
    announce(campaignId === null ? COPY.inactive : COPY.joining)
    void read()
  }

  // These handlers clear the actual nodes synchronously, before the browser
  // can retain a page for Back/Forward or present it after unlocking a phone.
  function suspend(): void {
    paused = true
    reset()
    clearSound()
  }

  function resume(): void {
    paused = document.visibilityState === 'hidden'
    if (!paused) recheck()
  }

  function onVisibility(): void {
    if (document.visibilityState !== 'hidden') resume()
    else if (audio.accepted) {
      // Drop the old stream's document picture too. The new background stream
      // keeps only slot sequences and sound until a fresh foreground snapshot.
      recheck()
    }
    else suspend()
  }

  function onPageShow(event: PageTransitionEvent): void {
    if (event.persisted) resume()
  }

  const identity = openIdentityBroadcast(() => {
    clearSound()
    audio.reset()
    signOut.hidden = true
    leave.hidden = true
    recheck()
  }, channelFactory)

  async function depart(kind: 'account' | 'screen'): Promise<void> {
    if (actionPending || departed) return
    actionPending = true
    reset()
    clearSound()
    signOut.disabled = true
    leave.disabled = true
    try {
      const options: RequestInit = { method: 'POST', cache: 'no-store' }
      if (kind === 'screen') {
        options.headers = { 'Content-Type': 'application/json' }
        options.body = JSON.stringify({ schema_version: CONTRACT_VERSION })
      }
      const response = await request(kind === 'account' ? '/auth/logout' : '/table/leave', options)
      if (!response.ok && !(kind === 'account' && response.status === 401)) throw new Error('Table exit failed')
      const focusWasOnAction = document.activeElement === signOut || document.activeElement === leave
      departed = true
      signOut.hidden = true
      leave.hidden = true
      reload.hidden = true
      if (kind === 'account') {
        identity.post()
        signIn.hidden = false
      }
      announce(kind === 'account' ? 'You signed out.' : 'You left the table.')
      if (focusWasOnAction) {
        if (kind === 'account') signIn.focus()
        else document.querySelector<HTMLElement>('a[href="/tavern"]')?.focus()
      }
    } catch {
      announce(kind === 'account' ? 'Couldn’t sign out. Try again.' : 'Couldn’t leave the table. Try again.')
    } finally {
      actionPending = false
      signOut.disabled = false
      leave.disabled = false
    }
  }

  const onSignOut = () => { void depart('account') }
  const onLeave = () => { void depart('screen') }
  reload.hidden = campaignId === null
  reload.addEventListener('click', recheck)
  signOut.addEventListener('click', onSignOut)
  leave.addEventListener('click', onLeave)
  document.addEventListener('visibilitychange', onVisibility)
  document.addEventListener('freeze', suspend)
  document.addEventListener('resume', resume)
  browser.addEventListener('pagehide', suspend)
  browser.addEventListener('pageshow', onPageShow)
  onVisibility()

  return () => {
    reset()
    clearSound()
    audio.dispose()
    identity.close()
    reload.removeEventListener('click', recheck)
    signOut.removeEventListener('click', onSignOut)
    leave.removeEventListener('click', onLeave)
    document.removeEventListener('visibilitychange', onVisibility)
    document.removeEventListener('freeze', suspend)
    document.removeEventListener('resume', resume)
    browser.removeEventListener('pagehide', suspend)
    browser.removeEventListener('pageshow', onPageShow)
  }
}
