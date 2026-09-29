/**
 * tableSessionApi -- the GM's table-session calls (agent-forge-harness-1kg.2.5.3,
 * brief section 7.9) on 1kg.2.10's `/campaigns/{campaign_id}/table-session`.
 *
 * The address names the campaign and nothing else (SEC-43). No link, join or
 * screen token exists in this client (SEC-41, TA-2): an answer is read through
 * `TableSessionAnswerSchema`, which keeps only the fields it names. No Rotate,
 * no screen mode (SEC-48, D-13). campaignApi's conventions otherwise: the cookie
 * only; a checked id, so a malformed one makes no request; JSON as
 * `application/json` (SEC-7); a result that never throws; a 401 is the
 * centralized sign-out; no server `message` is returned. Only `EndCourier` retries.
 */

import { notifyUnauthorized } from '../api'
import {
  CONTRACT_VERSION,
  readErrorBody,
  TableSessionAnswerSchema,
  TableSessionRequestSchema,
  type TableSession,
} from '../gm/contracts'
import { isOpaqueId } from './workspaceFragment'

export type TableSessionResult =
  | { readonly kind: 'ok'; readonly session: TableSession | null }
  /** The one 404 (another GM's, missing or archived campaign), or a malformed id. */
  | { readonly kind: 'unavailable' }
  /** 403: the `dm` gate, and later `start_gate` (`ubw`). The code is closed, never text. */
  | { readonly kind: 'refused'; readonly code: string | null }
  | { readonly kind: 'live_elsewhere' }
  | { readonly kind: 'throttled'; readonly retryAfterS: number | null }
  /** 5xx, a network failure, an unreadable body, anything unexpected. */
  | { readonly kind: 'failed' }
  | { readonly kind: 'unauthorized' }

export type TableSessionCommand =
  | { readonly action: 'start'; readonly commandId: string }
  | { readonly action: 'end'; readonly commandId: string; readonly sessionId: string }

const FAILED: TableSessionResult = { kind: 'failed' }
const UNAVAILABLE: TableSessionResult = { kind: 'unavailable' }

/** One id per intent (`^[A-Za-z0-9_-]{16,64}$`): 16 random bytes as base64url. */
export function mintCommandId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function call(fetchImpl: typeof fetch, campaignId: string, init?: RequestInit): Promise<TableSessionResult> {
  let res: Response
  try {
    res = await fetchImpl(`/campaigns/${encodeURIComponent(campaignId)}/table-session`, { ...init, credentials: 'include' })
  } catch {
    return FAILED
  }
  if (res.status === 401) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  const body: unknown = await res.json().catch(() => undefined)
  if (res.ok) {
    const answer = TableSessionAnswerSchema.safeParse(body)
    return answer.success ? { kind: 'ok', session: answer.data.session } : FAILED
  }
  if (res.status === 404) return UNAVAILABLE
  const error = readErrorBody(body)
  const info = error.kind === 'workbench' ? error.info : null
  if (res.status === 403) return { kind: 'refused', code: info?.code ?? null }
  if (res.status === 409 && info?.code === 'live_elsewhere') return { kind: 'live_elsewhere' }
  if (res.status === 429) return { kind: 'throttled', retryAfterS: info?.retry_after_s ?? null }
  return FAILED
}

/** `GET`: the live session, else the most recent, else `null`; a live row past
 * `ends_at` reads `ended` (SEC-42). */
export async function readTableSession(campaignId: string, fetchImpl: typeof fetch = fetch): Promise<TableSessionResult> {
  return isOpaqueId(campaignId) ? call(fetchImpl, campaignId) : UNAVAILABLE
}

/** `POST` Start or End, idempotent by command id. Sent once per call. */
export async function sendTableSession(
  campaignId: string,
  command: TableSessionCommand,
  fetchImpl: typeof fetch = fetch,
): Promise<TableSessionResult> {
  if (!isOpaqueId(campaignId)) return UNAVAILABLE
  const request = TableSessionRequestSchema.safeParse({
    schema_version: CONTRACT_VERSION,
    command_id: command.commandId,
    action: command.action,
    ...(command.action === 'end' ? { session_id: command.sessionId } : {}),
  })
  if (!request.success) return FAILED
  const headers = { 'Content-Type': 'application/json' }
  return call(fetchImpl, campaignId, { method: 'POST', headers, body: JSON.stringify(request.data) })
}

/** Listeners for `useSyncExternalStore`. */
export class Emitter {
  private readonly listeners = new Set<() => void>()

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => void this.listeners.delete(listener)
  }

  protected emit(): void {
    for (const listener of [...this.listeners]) listener()
  }
}

// ── End, until acknowledged (X-3, brief I-17, Critic 23a/23b) ────────────────

export type EndOutcome =
  | { readonly kind: 'ended'; readonly session: TableSession | null }
  | { readonly kind: 'gone' }
  | { readonly kind: 'signed_out' }

/** The wait after the first, second, ... failure; the last repeats. */
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]
/** setTimeout's ceiling: a longer delay would fire at once. */
export const MAX_TIMER_MS = 2_147_483_647

interface EndJob {
  readonly campaignId: string
  readonly sessionId: string
  readonly commandId: string
  readonly fetchImpl: typeof fetch
  readonly done: Promise<EndOutcome>
  readonly settle: (outcome: EndOutcome) => void
  failures: number
  sending: boolean
  timer: ReturnType<typeof setTimeout> | null
}

/**
 * Delivers Ends: one command id per session, retried after 1, 2, 4, 8, 16 s and
 * then every 30 s (or a 429's longer `retry_after_s`) until a 2xx, a 404 or a
 * 401. Keyed by session, not scope or component, so it keeps going after a
 * campaign switch and after its provider unmounts, until then or the tab closes.
 */
export class EndCourier extends Emitter {
  private readonly jobs = new Map<string, EndJob>()

  status(sessionId: string): 'idle' | 'sending' | 'waiting' {
    const job = this.jobs.get(sessionId)
    return job === undefined ? 'idle' : job.sending ? 'sending' : 'waiting'
  }

  /** A second press joins an End in flight and sends a waiting one at once. */
  send(campaignId: string, sessionId: string, fetchImpl: typeof fetch = fetch): Promise<EndOutcome> {
    const known = this.jobs.get(sessionId)
    if (known !== undefined) {
      if (!known.sending) this.attempt(known)
      return known.done
    }
    let settle: (outcome: EndOutcome) => void = () => undefined
    const done = new Promise<EndOutcome>((resolve) => (settle = resolve))
    const job: EndJob = { campaignId, sessionId, commandId: mintCommandId(), fetchImpl, done, settle, failures: 0, sending: false, timer: null }
    this.jobs.set(sessionId, job)
    this.attempt(job)
    return done
  }

  private attempt(job: EndJob): void {
    if (job.timer !== null) clearTimeout(job.timer)
    job.timer = null
    job.sending = true
    this.emit()
    const command = { action: 'end', commandId: job.commandId, sessionId: job.sessionId } as const
    void sendTableSession(job.campaignId, command, job.fetchImpl).then((result) => this.answered(job, result))
  }

  private answered(job: EndJob, result: TableSessionResult): void {
    job.sending = false
    if (result.kind === 'ok' || result.kind === 'unavailable' || result.kind === 'unauthorized') {
      this.jobs.delete(job.sessionId)
      this.emit()
      if (result.kind === 'ok') job.settle({ kind: 'ended', session: result.session })
      else job.settle({ kind: result.kind === 'unavailable' ? 'gone' : 'signed_out' })
      return
    }
    const backoff = BACKOFF_MS[Math.min(job.failures, BACKOFF_MS.length - 1)]
    const asked = result.kind === 'throttled' ? (result.retryAfterS ?? 0) * 1_000 : 0
    job.failures += 1
    job.timer = setTimeout(() => this.attempt(job), Math.min(Math.max(backoff, asked), MAX_TIMER_MS))
    this.emit()
  }
}
