import { CONTRACT_VERSION, type CueStopRequest } from '../gm/contracts'
import { Emitter, mintCommandId } from './tableSessionApi'

type Send = (body: CueStopRequest, signal: AbortSignal) => Promise<number>
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
      const status = await send(body, controller.signal).catch(() => 0)
      if (controller.signal.aborted) return
      if ((status >= 200 && status < 300) || (status >= 400 && status < 500 && status !== 408 && status !== 429)) {
        this.jobs.delete(key)
        settle(status < 300 ? 'acknowledged' : status === 404 || status === 401 ? 'gone' : 'failed')
      } else {
        job.timer = setTimeout(job.attempt, delay)
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
