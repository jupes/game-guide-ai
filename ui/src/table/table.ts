import { CONTRACT_VERSION } from '../gm/contracts'
import { openIdentityBroadcast, type IdentityChannelFactory } from '../shell/identityBroadcast'
import { tableProjections } from './projection'
import { mountTableAudio } from './audioControls'
import type { MixerCue } from './audioMixer'
import { tablePresence } from './presence'
import { responseClock, streamTable, tablePicture, type TablePicture } from './stream'

const POLL_MS = 5000
const BLANK_MS = 10_000
const READ_TIMEOUT_MS = 8000
const ANNOUNCEMENT_INTERVAL_MS = 100

type ReadResult = TablePicture | { kind: 'inactive' | 'signed-out' | 'retry' } | { kind: 'limited'; delay: number }

async function readTable(id: string, signal: AbortSignal, request: typeof fetch): Promise<ReadResult> {
  try {
    const sentAt = Date.now()
    const response = await request(`/table/snapshot?campaign_id=${encodeURIComponent(id)}`, { cache: 'no-store', signal })
    signal.throwIfAborted()
    if (response.status === 401) return { kind: 'signed-out' }
    if (response.status === 403 || response.status === 404) return { kind: 'inactive' }
    if (response.status === 429) {
      const seconds = Number(response.headers.get('Retry-After'))
      return { kind: 'limited', delay: Number.isFinite(seconds) ? Math.max(POLL_MS, Math.min(seconds, 300) * 1000) : POLL_MS }
    }
    if (!response.ok) return { kind: 'retry' }
    const offset = responseClock(response, sentAt)
    const snapshot = await response.json()
    signal.throwIfAborted()
    return tablePicture(snapshot, offset, response.headers.get('X-Table-Audio-Context'))
  } catch {
    return { kind: 'retry' }
  }
}

const COPY = {
  joining: 'Joining the table…',
  waiting: 'Nothing to see yet. Your GM hasn’t shared anything.',
  changed: 'Your GM shared something new.',
  stopped: 'Your GM stopped sharing something.',
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
  const picture = tableProjections(projections)
  const heading = document.querySelector<HTMLElement>('#player-table h1')!
  const reload = document.querySelector<HTMLButtonElement>('#table-reload')!
  const signIn = document.querySelector<HTMLAnchorElement>('#table-sign-in')!
  const signOut = document.querySelector<HTMLButtonElement>('#table-sign-out')!
  const leave = document.querySelector<HTMLButtonElement>('#table-leave')!
  const browser = document.defaultView!
  let clockOffsetMs = 0
  let presenceRefused = false
  const presence = tablePresence(campaignId, request, () => {
    presenceRefused = true
    clearSound()
    recheck()
  })
  const audio = mountTableAudio(document.querySelector<HTMLElement>('#table-sound')!, presence.update,
    () => Date.now() + clockOffsetMs)

  let activeRead: AbortController | null = null
  let pollTimer: number | undefined
  let blankTimer: number | undefined
  let soundTimer: number | undefined
  let resync = true
  let audioContext: string | null = null
  let hasPicture = false
  let streamRetryAt = 0
  let streamFailures = 0
  let paused = false
  let departed = false
  let actionPending = false
  let disposed = false

  const revealAnnouncements: string[] = []
  let announcementTimer: number | undefined

  function cancelAnnouncements(): void {
    browser.clearTimeout(announcementTimer)
    announcementTimer = undefined
    revealAnnouncements.length = 0
    delete status.dataset.repeat
  }

  function announce(message: string): void {
    cancelAnnouncements()
    if (status.textContent !== message) status.textContent = message
  }

  function deliverReveal(): void {
    announcementTimer = undefined
    const next = revealAnnouncements[0]
    if (next === undefined) return
    if (status.textContent === next) {
      // Leave the mounted live region empty for a separate rendering turn so
      // successive, identically worded reveals are distinct accessibility events.
      status.dataset.repeat = next
      status.textContent = ''
    } else {
      status.textContent = next
      delete status.dataset.repeat
      revealAnnouncements.shift()
    }
    announcementTimer = browser.setTimeout(deliverReveal, ANNOUNCEMENT_INTERVAL_MS)
  }

  function announceReveal(message: string): void {
    revealAnnouncements.push(message)
    if (announcementTimer === undefined) deliverReveal()
  }

  function restoreRedactedFocus(previous: Element | null): void {
    if (!previous || previous === document.body || (previous.isConnected && !previous.closest('[hidden]'))) return
    if (document.activeElement === previous || document.activeElement === document.body) heading.focus()
  }

  function showDegraded(degraded: boolean): void {
    const message = degraded ? COPY.degraded : ''
    if (connection.textContent !== message) connection.textContent = message
  }

  function redact(): void {
    cancelAnnouncements()
    const focused = document.activeElement
    picture.clear()
    hasPicture = false
    restoreRedactedFocus(focused)
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
    presenceRefused = false
    const focused = document.activeElement
    const nextContext = result.audioEnabled ? result.audioContext : null
    if (audioContext !== null && nextContext !== audioContext) {
      // A poll can be the first observation of a Rotate, reseat or new session.
      // Old media must stop before the first cue or listener report in the new scope.
      redact()
      clearSound()
    }
    audioContext = nextContext
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
    audio.show(audioContext !== null, slots, resync)
    presence.authorize(audioContext)
    resync = false
    if (document.visibilityState === 'hidden') {
      restoreRedactedFocus(focused)
      return
    }
    signOut.hidden = !result.participant
    leave.hidden = result.participant
    const change = picture.update(result.slots)
    const showing = projections.childElementCount > 0
    if (!hasPicture || !showing) {
      announce(showing ? '' : COPY.waiting)
    } else if (change !== 'unchanged') {
      announceReveal(change === 'shared' ? COPY.changed : COPY.stopped)
    } else if (status.textContent === COPY.reconnecting) {
      announce('')
    }
    hasPicture = true
    restoreRedactedFocus(focused)
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
    const focused = document.activeElement
    browser.clearTimeout(soundTimer)
    audioContext = null
    audio.hide()
    presence.reset()
    resync = true
    restoreRedactedFocus(focused)
  }

  function finish(kind: 'signed-out' | 'inactive'): void {
    const focused = document.activeElement
    browser.clearTimeout(blankTimer)
    redact()
    clearSound()
    showDegraded(false)
    // A revoked screen can lose its only credential when a presence response
    // reaches it before the stream's inactive frame. Both orders look alike.
    const offerSignIn = kind === 'signed-out' && !presenceRefused
    signIn.hidden = !offerSignIn
    if (!signIn.hidden) {
      signOut.hidden = true
      leave.hidden = true
    }
    announce(offerSignIn ? COPY.signedOut : COPY.inactive)
    restoreRedactedFocus(focused)
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
        streamFailures = 0
        showDegraded(false)
        showPicture(picture)
        hearFromTable()
      },
    })
    browser.clearTimeout(deadline)
    if (activeRead !== controller) return
    activeRead = null
    if (opened) redact()
    // A content-free terminal can mean Rotate, even during the opening
    // snapshot. Drop buffered media before either form of reauthorization.
    if (end.kind === 'reconnect') clearSound()
    if (end.kind === 'signed-out' || end.kind === 'inactive') {
      finish(end.kind)
    } else if (end.kind === 'reconnect' && ready) {
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
      showPicture(result)
      hearFromTable()
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
    const focused = document.activeElement
    reset()
    resync = true
    signIn.hidden = true
    announce(campaignId === null ? COPY.inactive : COPY.joining)
    restoreRedactedFocus(focused)
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
    const focused = document.activeElement
    presenceRefused = false
    clearSound()
    audio.reset()
    signOut.hidden = true
    leave.hidden = true
    recheck()
    restoreRedactedFocus(focused)
  }, channelFactory)

  async function depart(kind: 'account' | 'screen'): Promise<void> {
    if (actionPending || departed) return
    const action = kind === 'account' ? signOut : leave
    actionPending = true
    reset()
    clearSound()
    // Keep the pending action in the tab order. A native disabled button
    // drops focus on both mobile engines; actionPending guards re-entry.
    action.setAttribute('aria-disabled', 'true')
    try {
      const options: RequestInit = { method: 'POST', cache: 'no-store' }
      if (kind === 'screen') {
        options.headers = { 'Content-Type': 'application/json' }
        options.body = JSON.stringify({ schema_version: CONTRACT_VERSION })
      }
      const response = await request(kind === 'account' ? '/auth/logout' : '/table/leave', options)
      if (disposed) return
      if (!response.ok && !(kind === 'account' && response.status === 401)) throw new Error('Table exit failed')
      const focused = document.activeElement
      departed = true
      signOut.hidden = true
      leave.hidden = true
      reload.hidden = true
      if (kind === 'account') {
        identity.post()
        signIn.hidden = false
      }
      announce(kind === 'account' ? 'You signed out.' : 'You left the table.')
      if (focused === action) {
        if (kind === 'account') signIn.focus()
        else document.querySelector<HTMLElement>('a[href="/tavern"]')?.focus()
      } else restoreRedactedFocus(focused)
    } catch {
      if (!disposed) announce(kind === 'account' ? 'Couldn’t sign out. Try again.' : 'Couldn’t leave the table. Try again.')
    } finally {
      actionPending = false
      action.removeAttribute('aria-disabled')
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
    disposed = true
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
