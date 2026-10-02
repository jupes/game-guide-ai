/**
 * revealStop -- the Stop courier (agent-forge-harness-1kg.7.3, brief 3.2 and the
 * Critic's item 3), modelled on `tableSessionApi.EndCourier`.
 *
 * A Stop is a narrowing, so it is never gated, queued, debounced or confirmed by a
 * dialog (X-3). It goes out the moment it is pressed and is retried until the server
 * has answered for it: after 1, 2, 4, 8, 16 seconds and then every 30 (or a 429's
 * longer `retry_after_s`), always with the ONE command id minted for the press, so a
 * retry the server already applied is a replay and not a second Stop.
 *
 * Jobs are keyed by campaign and document (`*` for all). A second press while a
 * request is in flight joins it; a press while the job waits between retries sends
 * at once (the GM never waits on a backoff). A job ends only on a terminal answer
 * (`ok`, `gone`, `invalid`, `unauthorized`) or an account change (`retain`), never on
 * a scope change: a GM who presses Stop and then switches campaign must not leave the
 * table showing it. Nothing here touches web storage; durability across a reload is
 * REVEAL-16's, and PR-2's.
 */

import { Emitter, MAX_TIMER_MS, mintCommandId } from './tableSessionApi'
import { stopReveal, type StopResult } from '../gm/revealApi'
import type { RevealState } from '../gm/contracts'

/** The wait after the first, second, ... failure; the last repeats. */
export const STOP_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]

/** The document id a job carries for "stop everything". */
export const STOP_ALL = '*'

export type StopOutcome =
  | { readonly kind: 'stopped'; readonly state: RevealState | null }
  | { readonly kind: 'gone' }
  /** The server refused the request itself: retrying cannot help, and the table may still see it. */
  | { readonly kind: 'invalid' }
  | { readonly kind: 'signed_out' }

interface StopJob {
  /** The account that pressed Stop; another account never sends it. */
  readonly account: string | null
  readonly campaignId: string
  readonly documentId: string
  readonly key: string
  readonly commandId: string
  readonly fetchImpl: typeof fetch
  readonly done: Promise<StopOutcome>
  readonly settle: (outcome: StopOutcome) => void
  failures: number
  sending: boolean
  timer: ReturnType<typeof setTimeout> | null
}

const jobKey = (campaignId: string, documentId: string): string => `${campaignId}:${documentId}`

export class StopCourier extends Emitter {
  private readonly jobs = new Map<string, StopJob>()
  /** Bumps on every change, so a `useSyncExternalStore` snapshot is a plain number. */
  version = 0

  protected override emit(): void {
    this.version += 1
    super.emit()
  }

  status(campaignId: string, documentId: string): 'idle' | 'sending' | 'waiting' {
    const job = this.jobs.get(jobKey(campaignId, documentId))
    return job === undefined ? 'idle' : job.sending ? 'sending' : 'waiting'
  }

  /** The document ids (or `*`) with an unacknowledged Stop in this campaign. */
  pending(campaignId: string): string[] {
    return [...this.jobs.values()].filter((job) => job.campaignId === campaignId).map((job) => job.documentId)
  }

  /** A Stop in this campaign has failed at least once and is retrying. */
  failing(campaignId: string): boolean {
    return [...this.jobs.values()].some((job) => job.campaignId === campaignId && job.failures > 0)
  }

  send(
    campaignId: string,
    documentId: string,
    fetchImpl: typeof fetch = fetch,
    account: string | null = null,
  ): Promise<StopOutcome> {
    this.retain(account)
    const key = jobKey(campaignId, documentId)
    const known = this.jobs.get(key)
    if (known !== undefined) {
      if (!known.sending) this.attempt(known)
      return known.done
    }
    let settle: (outcome: StopOutcome) => void = () => undefined
    const done = new Promise<StopOutcome>((resolve) => (settle = resolve))
    const job: StopJob = {
      account,
      campaignId,
      documentId,
      key,
      commandId: mintCommandId(),
      fetchImpl,
      done,
      settle,
      failures: 0,
      sending: false,
      timer: null,
    }
    this.jobs.set(key, job)
    this.attempt(job)
    return done
  }

  /** Drops every Stop another account pressed: `signed_out`, never sent again, and an answer still in flight is ignored. */
  retain(account: string | null): void {
    const others = [...this.jobs.values()].filter((job) => job.account !== account)
    for (const job of others) {
      if (job.timer !== null) clearTimeout(job.timer)
      this.jobs.delete(job.key)
      job.settle({ kind: 'signed_out' })
    }
    if (others.length > 0) this.emit()
  }

  private attempt(job: StopJob): void {
    if (job.timer !== null) clearTimeout(job.timer)
    job.timer = null
    job.sending = true
    this.emit()
    const stop =
      job.documentId === STOP_ALL
        ? ({ commandId: job.commandId, scope: 'all' } as const)
        : ({ commandId: job.commandId, scope: 'document', documentId: job.documentId } as const)
    void stopReveal(job.campaignId, stop, job.fetchImpl).then((result) => this.answered(job, result))
  }

  private answered(job: StopJob, result: StopResult): void {
    if (this.jobs.get(job.key) !== job) return
    job.sending = false
    const ended = this.outcomeOf(result)
    if (ended !== null) {
      this.jobs.delete(job.key)
      this.emit()
      job.settle(ended)
      return
    }
    const backoff = STOP_BACKOFF_MS[Math.min(job.failures, STOP_BACKOFF_MS.length - 1)]
    const asked = result.kind === 'throttled' ? (result.retryAfterS ?? 0) * 1_000 : 0
    job.failures += 1
    job.timer = setTimeout(() => this.attempt(job), Math.min(Math.max(backoff, asked), MAX_TIMER_MS))
    this.emit()
  }

  /** The terminal outcome of an answer, or `null` when the Stop must be tried again. */
  private outcomeOf(result: StopResult): StopOutcome | null {
    switch (result.kind) {
      case 'ok':
        return { kind: 'stopped', state: result.state }
      case 'gone':
        return { kind: 'gone' }
      case 'invalid':
        return { kind: 'invalid' }
      case 'unauthorized':
        return { kind: 'signed_out' }
      default:
        return null
    }
  }
}
