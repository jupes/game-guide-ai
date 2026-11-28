import { CONTRACT_VERSION } from '../gm/contracts'
import { openIdentityBroadcast, type IdentityChannelFactory } from '../shell/identityBroadcast'
import { isSignedOutResponse, readTableIdentity, type IdentityResult, type TableActor } from './identity'
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
  inactive: 'This table is unavailable.',
  signedOut: 'Sign in to open this table.',
  limited: 'Too many requests. Wait a moment and try again.',
  full: 'This table is full. Ask your GM to make room.',
  identityUnavailable: 'Couldn’t check how you’re signed in. Try again.',
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
  const returnLink = document.querySelector<HTMLAnchorElement>('#table-return')!
  const identityNotice = document.querySelector<HTMLElement>('#table-identity-notice')!
  const browser = document.defaultView!
  let clockOffsetMs = 0
  const presence = tablePresence(campaignId, request, () => {
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
  let consentToRecheck: string | null = null
  let hasPicture = false
  let streamRetryAt = 0
  let streamFailures = 0
  let paused = false
  let departed = false
  let pendingAction: AbortController | null = null
  let actionDeadline: number | undefined
  let disposed = false
  let credential: TableActor | 'signed-out' | 'unknown' = 'unknown'
  let lastVerified: TableActor | 'signed-out' | null = null
  let wasScreen = false
  let identityRead: AbortController | null = null
  let identityTimer: number | undefined
  let identityRetryAt = 0

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

  function showActions(retry = false): void {
    const focused = document.activeElement
    signOut.hidden = credential !== 'account'
    leave.hidden = credential !== 'screen'
    signIn.hidden = credential !== 'signed-out' || wasScreen
    returnLink.textContent = credential === 'account' ? 'Back to your seats' : 'Back to the Tavern'
    reload.hidden = !retry && campaignId === null
    reload.textContent = retry ? 'Retry' : 'Reload table'
    if (retry) reload.setAttribute('aria-describedby', 'table-identity-notice')
    else reload.removeAttribute('aria-describedby')
    if (retry && Date.now() < identityRetryAt) reload.setAttribute('aria-disabled', 'true')
    else reload.removeAttribute('aria-disabled')
    restoreRedactedFocus(focused)
  }

  function cancelIdentityRead(): void {
    browser.clearTimeout(identityTimer)
    identityRead?.abort()
    identityRead = null
  }

  async function checkIdentity(): Promise<IdentityResult | null> {
    cancelIdentityRead()
    if (Date.now() < identityRetryAt) return { kind: 'unavailable', retryAt: identityRetryAt }
    const controller = new AbortController()
    identityRead = controller
    const deadline = browser.setTimeout(() => controller.abort(), READ_TIMEOUT_MS)
    const result = await readTableIdentity(request, controller.signal)
    browser.clearTimeout(deadline)
    if (identityRead !== controller) return null
    identityRead = null
    identityRetryAt = result.kind === 'unavailable' ? result.retryAt ?? 0 : 0
    return result
  }

  function applyIdentity(result: IdentityResult): void {
    const previous = lastVerified
    const wasUnknown = credential === 'unknown'
    credential = result.kind === 'unavailable' ? 'unknown' : result.kind
    if (credential !== 'unknown') lastVerified = credential
    if (credential === 'account' || credential === 'screen') wasScreen = credential === 'screen'
    identityNotice.hidden = credential !== 'unknown'
    identityNotice.textContent = credential === 'unknown' ? (identityRetryAt ? COPY.limited : COPY.identityUnavailable) : ''
    showActions(credential === 'unknown')
    if (credential === 'unknown') {
      const delay = identityRetryAt ? Math.max(0, identityRetryAt - Date.now()) : POLL_MS
      identityTimer = browser.setTimeout(refreshIdentity, Math.min(delay, 2_147_483_647))
    } else if (credential === 'signed-out') {
      reset()
      clearSound()
      resetConsent()
      announce(wasScreen ? COPY.inactive : COPY.signedOut)
    } else if ((previous !== null && credential !== previous)
      || (wasUnknown && (activeRead !== null || hasPicture || audioContext !== null))) {
      // A request opened while identity was unknown may still belong to
      // another cookie, even if it has not delivered its first frame yet.
      const sameActorPossible = previous === null || credential === previous
      const priorConsent = wasUnknown && sameActorPossible && audio.accepted ? audioContext : null
      reset()
      clearSound()
      if (priorConsent !== null) consentToRecheck = priorConsent
      else resetConsent()
      announce(campaignId === null ? COPY.inactive : COPY.joining)
      void read()
    }
  }

  async function refreshIdentity(): Promise<void> {
    if (paused || departed || disposed || pendingAction) return
    const result = await checkIdentity()
    if (result) applyIdentity(result)
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

  function showPicture(result: TablePicture): boolean {
    // A screen can only receive the public slot. A contradictory read can
    // race a cookie change. Drop its entire picture before touching the DOM,
    // audio or presence, then recheck the credential and table independently.
    if (lastVerified === 'screen' && result.slots.some(slot => slot.slot === 'mine')) {
      reset()
      clearSound()
      resetConsent()
      announce(COPY.inactive)
      queue(POLL_MS)
      void refreshIdentity()
      return false
    }
    const focused = document.activeElement
    const nextContext = result.audioEnabled ? result.audioContext : null
    if (consentToRecheck !== null) {
      // The server binds this value to the effective account/seat or grant
      // as well as the exact session generation. Keep only that actor's tap.
      if (consentToRecheck !== nextContext) resetConsent()
      consentToRecheck = null
    }
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
      return true
    }
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
    return true
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

  function resetConsent(): void {
    consentToRecheck = null
    audio.reset()
  }

  async function finish(): Promise<void> {
    browser.clearTimeout(blankTimer)
    redact()
    clearSound()
    showDegraded(false)
    announce(COPY.inactive)
    await refreshIdentity()
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
        if (activeRead !== controller) return
        opened = true
        resync = true
        redact()
        watchBytes()
      },
      bytes() { if (activeRead === controller) watchBytes() },
      picture(picture) {
        if (activeRead !== controller) return
        ready = true
        browser.clearTimeout(deadline)
        streamFailures = 0
        showDegraded(false)
        if (showPicture(picture)) hearFromTable()
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
      await finish()
    } else if (end.kind === 'reconnect' && ready) {
      recheck()
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
    if (campaignId === null || paused || departed || pendingAction || disposed || credential === 'signed-out' || activeRead !== null) return
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
      if (!showPicture(result)) return
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
    await finish()
  }

  function recheck(): void {
    if (disposed || departed || paused || pendingAction) return
    reset()
    resync = true
    announce(campaignId === null ? COPY.inactive : COPY.joining)
    void refreshIdentity()
    // The picture starts on its own turn even if identity is offline. An
    // already-resolved identity can establish the scope before opening it.
    queue(0)
  }

  // These handlers clear the actual nodes synchronously, before the browser
  // can retain a page for Back/Forward or present it after unlocking a phone.
  function suspend(): void {
    paused = true
    cancelIdentityRead()
    cancelAction()
    reset()
    clearSound()
  }

  function resume(): void {
    paused = document.visibilityState === 'hidden'
    if (!paused) recheck()
  }

  function onVisibility(): void {
    if (document.visibilityState !== 'hidden') resume()
    else if (audio.accepted && !pendingAction) {
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
    if (disposed) return
    cancelAction()
    cancelIdentityRead()
    reset()
    clearSound()
    resetConsent()
    credential = 'unknown'
    lastVerified = null
    wasScreen = false
    departed = false
    identityNotice.hidden = true
    showActions()
    recheck()
  }, channelFactory)

  function cancelAction(): void {
    browser.clearTimeout(actionDeadline)
    pendingAction?.abort()
    pendingAction = null
    signOut.removeAttribute('aria-disabled')
    leave.removeAttribute('aria-disabled')
  }

  async function depart(kind: TableActor): Promise<void> {
    if (pendingAction || departed || disposed || paused || credential !== kind) return
    const action = kind === 'account' ? signOut : leave
    const failed = kind === 'account' ? 'Couldn’t sign out. Try again.' : 'Couldn’t leave the table. Try again.'
    const controller = new AbortController()
    pendingAction = controller
    cancelIdentityRead()
    reset()
    clearSound()
    // Keep the pending action in the tab order. A native disabled button
    // drops focus on both mobile engines; the pending request guards re-entry.
    action.setAttribute('aria-disabled', 'true')
    try {
      if (kind === 'screen') {
        const current = await checkIdentity()
        if (!current || pendingAction !== controller) return
        if (current.kind !== kind) {
          cancelAction()
          applyIdentity(current)
          if (current.kind === 'unavailable') void read()
          return
        }
      }
      const options: RequestInit = { method: 'POST', cache: 'no-store', redirect: 'error', signal: controller.signal }
      if (kind === 'screen') {
        options.headers = { 'Content-Type': 'application/json' }
        options.body = JSON.stringify({ schema_version: CONTRACT_VERSION })
      }
      actionDeadline = browser.setTimeout(() => {
        if (pendingAction !== controller) return
        cancelAction()
        announce(failed)
      }, READ_TIMEOUT_MS)
      const response = await request(kind === 'account' ? '/table/sign-out' : '/table/leave', options)
      if (pendingAction !== controller) return
      if (kind === 'account' && response.status === 409) {
        // The guarded POST observed a screen. Hide exits until the separate
        // credential read settles, and allow only public pictures meanwhile.
        cancelAction()
        resetConsent()
        credential = 'unknown'
        lastVerified = 'screen'
        wasScreen = true
        showActions()
        announce(campaignId === null ? COPY.inactive : COPY.joining)
        const fresh = await checkIdentity()
        if (fresh) {
          applyIdentity(fresh)
          void read()
        }
        return
      }
      if (response.status !== 204 && !(kind === 'account' && await isSignedOutResponse(response))) throw new Error('Table exit failed')
      if (pendingAction !== controller) return
      const focused = document.activeElement
      departed = true
      credential = kind === 'account' ? 'signed-out' : 'unknown'
      returnLink.textContent = 'Back to the Tavern'
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
        else returnLink.focus()
      } else restoreRedactedFocus(focused)
    } catch {
      if (pendingAction === controller) announce(failed)
    } finally {
      if (pendingAction === controller) cancelAction()
    }
  }

  const onSignOut = () => { void depart('account') }
  const onLeave = () => { void depart('screen') }
  const onReload = () => {
    if (credential === 'unknown' && !identityNotice.hidden) void refreshIdentity()
    else recheck()
  }
  reload.hidden = campaignId === null
  reload.addEventListener('click', onReload)
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
    cancelAction()
    cancelIdentityRead()
    reset()
    clearSound()
    audio.dispose()
    identity.close()
    reload.removeEventListener('click', onReload)
    signOut.removeEventListener('click', onSignOut)
    leave.removeEventListener('click', onLeave)
    document.removeEventListener('visibilitychange', onVisibility)
    document.removeEventListener('freeze', suspend)
    document.removeEventListener('resume', resume)
    browser.removeEventListener('pagehide', suspend)
    browser.removeEventListener('pageshow', onPageShow)
  }
}
