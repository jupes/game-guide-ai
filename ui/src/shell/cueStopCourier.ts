import { CONTRACT_VERSION, type CueStopRequest } from '../gm/contracts'
import { Emitter, MAX_TIMER_MS, mintCommandId } from './tableSessionApi'

type Send = (body: CueStopRequest, signal: AbortSignal) => Promise<Response>
type Outcome = 'acknowledged' | 'gone' | 'failed'
interface Job {
  account: string
  path: string
  controller: AbortController
  done: Promise<Outcome>
  settle: (value: Outcome) => void
  timer?: ReturnType<typeof setTimeout>
  attempt: () => void
}

async function retryAfter(response: Response): Promise<number> {
  const header = response.headers.get('Retry-After')?.trim()
  if (header && /^\d+$/.test(header)) return Number(header) * 1000
  if (header && Number.isFinite(Date.parse(header))) return Math.max(0, Date.parse(header) - Date.now())
  if (response.status !== 429) return 0
  const details = await response.json().catch(() => null)
  const seconds = details?.detail?.retry_after_s
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0
}

/** A Stop belongs to the account and session where it was pressed, even after navigation. */
export class CueStopCourier extends Emitter {
  private jobs = new Map<string, Job>()

  retain(account: string | null): void {
    for (const [key, job] of this.jobs) {
      if (job.account !== account) {
        job.controller.abort()
        clearTimeout(job.timer)
        this.jobs.delete(key)
        job.settle('gone')
        this.emit()
      }
    }
  }

  status(account: string, path: string): { pendingStops: number; retrying: boolean } {
    const jobs = [...this.jobs.values()].filter((job) => job.account === account && job.path === path)
    return { pendingStops: jobs.length, retrying: jobs.some((job) => job.timer !== undefined) }
  }

  send(account: string, path: string, cue: string | null, send: Send): Promise<Outcome> {
    const key = JSON.stringify([account, path, cue])
    const existing = this.jobs.get(key)
    if (existing) {
      if (existing.timer !== undefined) existing.attempt()
      return existing.done
    }
    const body: CueStopRequest = { schema_version: CONTRACT_VERSION, command_id: mintCommandId(), cue_id: cue }
    const controller = new AbortController()
    let delay = 500
    let settle!: (value: Outcome) => void
    const done = new Promise<Outcome>((resolve) => { settle = resolve })
    const job: Job = { account, path, done, settle, controller, attempt: () => { void attempt() } }
    const attempt = async () => {
      clearTimeout(job.timer)
      job.timer = undefined
      const response = await send(body, controller.signal).catch(() => null)
      const status = response?.status ?? 0
      if (controller.signal.aborted) return
      if ((status >= 200 && status < 300) || (status >= 400 && status < 500 && status !== 408 && status !== 429)) {
        this.jobs.delete(key)
        settle(status < 300 ? 'acknowledged' : status === 404 || status === 401 ? 'gone' : 'failed')
      } else {
        const asked = response ? await retryAfter(response) : 0
        if (controller.signal.aborted) return
        job.timer = setTimeout(job.attempt, Math.min(MAX_TIMER_MS, Math.max(delay, asked)))
        delay = Math.min(delay * 2, 5000)
      }
      this.emit()
    }
    this.jobs.set(key, job)
    this.emit()
    job.attempt()
    return done
  }
}

export const cueStops = new CueStopCourier()
