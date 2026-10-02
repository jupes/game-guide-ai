/**
 * revealStop.test.ts -- the in-memory retrying Stop courier
 * (agent-forge-harness-1kg.7.3, brief 3.2 and test 13, and the Critic's item 3).
 * Fake timers; the real `stopReveal` over a recording fetch, so the command id and
 * the body are what really would be sent.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StopCourier, STOP_BACKOFF_MS } from './revealStop'

const CAMPAIGN = 'cmp_stopCourierCampaign001'
const OTHER = 'cmp_stopCourierOther0000001'
const DOC = 'doc_stopCourierDocument0001'
const DOC_B = 'doc_stopCourierDocument0002'

type Reply = { status: number; body?: unknown }

function recorder(replies: Reply[]) {
  const sent: Array<{ campaign: string; body: Record<string, unknown> }> = []
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const reply = replies[Math.min(sent.length, replies.length - 1)]
    const campaign = /campaigns\/([^/]+)\//.exec(String(input))?.[1] ?? ''
    sent.push({ campaign, body: JSON.parse(String(init?.body)) as Record<string, unknown> })
    return new Response(JSON.stringify(reply.body ?? {}), { status: reply.status })
  }) as unknown as typeof fetch
  return { sent, fetchImpl }
}

const OK: Reply = { status: 200, body: { schema_version: 1, state: null } }
const FAIL: Reply = { status: 503 }

const flush = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0)
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('a Stop is sent at once', () => {
  it('sends one request on the press and settles stopped on a 2xx', async () => {
    const courier = new StopCourier()
    const r = recorder([OK])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    expect(r.sent).toHaveLength(1)
    expect(r.sent[0].body).toMatchObject({ scope: 'document', document_id: DOC })
    expect(r.sent[0].body).not.toHaveProperty('reveal_epoch')
    expect(await done).toEqual({ kind: 'stopped', state: null })
    expect(courier.status(CAMPAIGN, DOC)).toBe('idle')
  })

  it('names `all` with no document', async () => {
    const courier = new StopCourier()
    const r = recorder([OK])
    void courier.send(CAMPAIGN, '*', r.fetchImpl, 'u1')
    await flush()
    expect(r.sent[0].body).toMatchObject({ scope: 'all' })
    expect(r.sent[0].body).not.toHaveProperty('document_id')
  })
})

describe('retries (test 13)', () => {
  it('retries after 1, 2, 4, 8, 16 then every 30 seconds with the SAME command id', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    expect(STOP_BACKOFF_MS).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 30_000])
    expect(r.sent).toHaveLength(1)
    for (const [index, wait] of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000].entries()) {
      await vi.advanceTimersByTimeAsync(wait - 1)
      expect(r.sent).toHaveLength(index + 1)
      await vi.advanceTimersByTimeAsync(1)
      expect(r.sent).toHaveLength(index + 2)
    }
    // Mutation: minting a new id per retry fails this.
    expect(new Set(r.sent.map((s) => s.body.command_id)).size).toBe(1)
    expect(courier.status(CAMPAIGN, DOC)).not.toBe('idle')
    expect(courier.failing(CAMPAIGN)).toBe(true)
  })

  it('stops retrying on a 2xx, and settles', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL, OK])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(await done).toEqual({ kind: 'stopped', state: null })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(r.sent).toHaveLength(2)
    expect(courier.failing(CAMPAIGN)).toBe(false)
  })

  it('stops on a 404 and does not retry it (gone)', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL, { status: 404 }])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(await done).toEqual({ kind: 'gone' })
    await vi.advanceTimersByTimeAsync(120_000)
    // Mutation: retrying after a 404 would send a third.
    expect(r.sent).toHaveLength(2)
  })

  it('stops on a 422 and says it did not take (invalid), never a silent success', async () => {
    const courier = new StopCourier()
    const r = recorder([{ status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false } } }])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    expect(await done).toEqual({ kind: 'invalid' })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(r.sent).toHaveLength(1)
    expect(courier.status(CAMPAIGN, DOC)).toBe('idle')
  })

  it('stops on a 401 (the shell signs out) and settles signed_out', async () => {
    const courier = new StopCourier()
    const r = recorder([{ status: 401 }])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    expect(await done).toEqual({ kind: 'signed_out' })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(r.sent).toHaveLength(1)
  })

  it('waits the longer of the backoff and a 429 retry_after_s', async () => {
    const courier = new StopCourier()
    const throttled: Reply = { status: 429, body: { detail: { code: 'throttled', message: 'x', retryable: true, retry_after_s: 40 } } }
    const r = recorder([throttled, OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    await vi.advanceTimersByTimeAsync(39_999)
    expect(r.sent).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(r.sent).toHaveLength(2)
  })

  it('does not wait less than the backoff when retry_after_s is shorter', async () => {
    const courier = new StopCourier()
    const throttled: Reply = { status: 429, body: { detail: { code: 'throttled', message: 'x', retryable: true, retry_after_s: 0 } } }
    const r = recorder([throttled, OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    await vi.advanceTimersByTimeAsync(999)
    expect(r.sent).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(r.sent).toHaveLength(2)
  })
})

describe('presses', () => {
  it('a second press while a request is in flight joins it: one request, one id', async () => {
    const courier = new StopCourier()
    const gate = { release: (): void => undefined }
    const sent: unknown[] = []
    const fetchImpl = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      sent.push(JSON.parse(String(init?.body)))
      await new Promise<void>((resolve) => (gate.release = resolve))
      return new Response(JSON.stringify({ schema_version: 1, state: null }), { status: 200 })
    }) as typeof fetch
    const first = courier.send(CAMPAIGN, DOC, fetchImpl, 'u1')
    await flush()
    expect(courier.status(CAMPAIGN, DOC)).toBe('sending')
    const second = courier.send(CAMPAIGN, DOC, fetchImpl, 'u1')
    await flush()
    expect(sent).toHaveLength(1)
    gate.release()
    expect(await first).toEqual(await second)
  })

  it('a press while the job waits between retries sends NOW with the same id', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL, FAIL, OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(r.sent).toHaveLength(2)
    expect(courier.status(CAMPAIGN, DOC)).toBe('waiting')
    // The job is now waiting 2 s; pressing again does not make the GM wait.
    const again = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    expect(r.sent).toHaveLength(3)
    expect(r.sent[2].body.command_id).toBe(r.sent[0].body.command_id)
    expect(await again).toEqual({ kind: 'stopped', state: null })
    // The abandoned timer never sends a fourth.
    await vi.advanceTimersByTimeAsync(120_000)
    expect(r.sent).toHaveLength(3)
  })

  it('a new press after a job ended mints a new command id', async () => {
    const courier = new StopCourier()
    const r = recorder([OK])
    await courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    expect(r.sent).toHaveLength(2)
    expect(r.sent[1].body.command_id).not.toBe(r.sent[0].body.command_id)
  })

  it('keys jobs by campaign and document: two documents are two jobs with two ids', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    void courier.send(CAMPAIGN, DOC_B, r.fetchImpl, 'u1')
    void courier.send(OTHER, DOC, r.fetchImpl, 'u1')
    await flush()
    expect(r.sent).toHaveLength(3)
    expect(new Set(r.sent.map((s) => s.body.command_id)).size).toBe(3)
    expect(courier.pending(CAMPAIGN).sort()).toEqual([DOC, DOC_B].sort())
    expect(courier.pending(OTHER)).toEqual([DOC])
  })
})

describe('what ends a job', () => {
  it('survives a scope change: pressing for another campaign does not end it, and the retry still fires', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    // The GM switches campaign and presses a Stop there: the first campaign's job is untouched.
    void courier.send(OTHER, DOC, r.fetchImpl, 'u1')
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    const forFirst = r.sent.filter((s) => s.campaign === CAMPAIGN)
    expect(forFirst.length).toBeGreaterThanOrEqual(2)
    expect(courier.pending(CAMPAIGN)).toEqual([DOC])
  })

  it('an account change drops every job another account pressed: signed_out, never sent again', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    courier.retain('u2')
    expect(await done).toEqual({ kind: 'signed_out' })
    await vi.advanceTimersByTimeAsync(120_000)
    expect(r.sent).toHaveLength(1)
    expect(courier.pending(CAMPAIGN)).toEqual([])
  })

  it('retain for the same account keeps its jobs', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL, OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    courier.retain('u1')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(r.sent).toHaveLength(2)
  })

  it('tells subscribers when a job starts, fails and ends', async () => {
    const courier = new StopCourier()
    const seen: number[] = []
    courier.subscribe(() => seen.push(courier.version))
    const r = recorder([FAIL, OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(seen.length).toBeGreaterThanOrEqual(3)
    expect(new Set(seen).size).toBe(seen.length)
  })
})

describe('replaying a marker (REVEAL-16, PR-2)', () => {
  it('sends with the command id it is given, so a replay the server already applied is a replay', async () => {
    const courier = new StopCourier()
    const r = recorder([OK])
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1', 'cmd_replayedFromMarker001')
    await flush()
    expect(r.sent[0].body).toMatchObject({ command_id: 'cmd_replayedFromMarker001' })
  })

  it('names the command id of a job in flight, and none once it ended', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL, OK])
    const done = courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    const id = courier.commandIdOf(CAMPAIGN, DOC)
    expect(id).toBe(r.sent[0].body.command_id)
    expect(courier.commandIdOf(CAMPAIGN, DOC_B)).toBeNull()
    await vi.advanceTimersByTimeAsync(1_000)
    await done
    expect(courier.commandIdOf(CAMPAIGN, DOC)).toBeNull()
  })

  it('a press while the job is already in flight keeps its id, even when another is offered', async () => {
    const courier = new StopCourier()
    const r = recorder([FAIL]) // never ends
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1')
    await flush()
    const first = courier.commandIdOf(CAMPAIGN, DOC)
    void courier.send(CAMPAIGN, DOC, r.fetchImpl, 'u1', 'cmd_neverUsedBecauseJobExists')
    await flush()
    expect(courier.commandIdOf(CAMPAIGN, DOC)).toBe(first)
  })

  it('reports an unacknowledged Stop in any campaign, for the unload guard', async () => {
    const courier = new StopCourier()
    expect(courier.anyPending()).toBe(false)
    const r = recorder([FAIL, OK])
    void courier.send(OTHER, DOC, r.fetchImpl, 'u1')
    await flush()
    expect(courier.anyPending()).toBe(true)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(courier.anyPending()).toBe(false)
  })
})
