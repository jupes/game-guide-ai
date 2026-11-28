import { CONTRACT_VERSION } from '../gm/contracts'

type State = 'pending' | 'idle' | 'listening' | 'muted' | 'absent'
const HEARTBEAT_MS = 15_000
const HANDLE = /^\/table\/assets\/([A-Za-z0-9_-]{32})$/

/** One page, one report in flight. A registration is useful only with the same live principal. */
export function tablePresence(campaign: string | null, request: typeof fetch, refresh: () => void) {
  const path = `/table/presence?campaign_id=${encodeURIComponent(campaign ?? '')}`
  let pageId: string | null = null
  let context: string | null = null
  let registration: string | null = null
  let revision = 0
  let report = { state: 'pending' as State, playing_handles: [] as string[] }
  let busy: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let retryAt = 0
  let refusedContext: string | null = null
  let retryOpenAt = 0

  function options(body: object): RequestInit {
    return { method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema_version: CONTRACT_VERSION, ...body }) }
  }

  function close(id: string): void {
    void request(path, { ...options({ action: 'close', registration: id }), keepalive: true }).catch(() => {})
  }

  function reset(): void {
    clearTimeout(timer)
    // A pending open may have committed. Retire it locally now and let its
    // bounded response supply the registration needed to close that row.
    if (registration) busy?.abort()
    busy = null
    const previous = registration
    registration = context = pageId = null
    retryAt = revision = 0
    if (previous) close(previous)
  }

  function schedule(delay: number): void {
    clearTimeout(timer)
    if (context) timer = setTimeout(() => { void send() }, Math.max(delay, retryAt - Date.now()))
  }

  async function send(): Promise<void> {
    if (busy || !context || !pageId || report.state === 'absent') return
    const controller = new AbortController()
    busy = controller
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)])
    const opening = registration === null
    const sent = report
    const body = opening ? { action: 'open', page_id: pageId, context }
      : { action: 'report', registration, revision: ++revision, ...sent }
    let delay = HEARTBEAT_MS
    try {
      const response = await request(path, { ...options(body), signal })
      if (opening && response.ok) {
        const answer = await response.json()
        if (answer?.schema_version === CONTRACT_VERSION && typeof answer.registration === 'string' &&
            /^[A-Za-z0-9_-]{32}$/.test(answer.registration)) {
          if (busy !== controller) close(answer.registration)
          else {
            registration = answer.registration
            delay = 0
          }
        }
        return
      }
      if (busy !== controller) return
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        refusedContext = context
        retryOpenAt = Date.now() + HEARTBEAT_MS
        reset()
        refresh()
        return
      }
      if (!response.ok) {
        const seconds = Number(response.headers.get('Retry-After'))
        retryAt = Date.now() + Math.max(HEARTBEAT_MS, Number.isFinite(seconds) ? Math.min(300, seconds) * 1000 : 0)
      }
    } catch { /* Rejoin on the next heartbeat. Table playback has its own transport. */ }
    finally {
      if (busy === controller) {
        busy = null
        schedule(report !== sent ? 0 : delay)
      }
    }
  }

  return {
    authorize(next: string | null) {
      if (next === context) return
      reset()
      if (campaign && next && /^[a-f0-9]{64}$/.test(next)) {
        context = next
        pageId = crypto.randomUUID()
        retryAt = next === refusedContext ? retryOpenAt : 0
        schedule(0)
      }
    },
    update(state: State, urls: readonly string[]) {
      const handles = Array.from(new Set(urls.flatMap((url) => {
        const match = HANDLE.exec(url)
        return match ? [match[1]] : []
      }))).sort().slice(0, 2)
      const playing = state === 'listening' ? handles : []
      const observed = state === 'listening' && !playing.length ? 'idle' : state
      if (report.state === observed && playing.join() === report.playing_handles.join()) return
      report = { state: observed, playing_handles: playing }
      if (observed === 'absent') reset()
      else schedule(0)
    },
    reset,
  }
}
