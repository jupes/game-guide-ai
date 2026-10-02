/**
 * lossGuard.test.ts -- the pure core of the loss guard (agent-forge-harness-1kg.6.3,
 * T-7; CANVAS-16, CANVAS-17, §5.3). The dialog is `LossGuardDialog.test.tsx`; the
 * provider that wires both is `canvasContext.test.tsx`.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { FLUSH_TIMEOUT_MS, guardStep, runGuard, type CanvasDirtySource, type FlushOutcome } from './lossGuard'

function source(dirty: boolean, outcome: FlushOutcome | 'hang' | 'throw' = 'saved'): CanvasDirtySource & {
  flushes: number[]
} {
  const flushes: number[] = []
  return {
    flushes,
    isDirty: () => dirty,
    flush: (timeoutMs) => {
      flushes.push(timeoutMs)
      if (outcome === 'throw') return Promise.reject(new Error('flush blew up'))
      if (outcome === 'hang') return new Promise<FlushOutcome>(() => {})
      return Promise.resolve(outcome)
    },
    discard: vi.fn(),
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('guardStep', () => {
  it('maps every flush outcome to what the guard does next', () => {
    expect(guardStep('saved')).toBe('proceed')
    expect(guardStep('offline')).toBe('refuse-offline')
    expect(guardStep('failed')).toBe('ask')
    expect(guardStep('retryable')).toBe('ask')
  })

  it('waits at most five seconds for a flush (§5.3)', () => {
    expect(FLUSH_TIMEOUT_MS).toBe(5000)
  })
})

describe('runGuard', () => {
  it('proceeds without flushing when there is no source, or none is dirty (no dialog)', async () => {
    expect(await runGuard([])).toEqual({ kind: 'proceed' })
    const clean = source(false, 'failed')
    expect(await runGuard([clean])).toEqual({ kind: 'proceed' })
    expect(clean.flushes).toEqual([])
  })

  it('flushes a dirty source with the timeout, and proceeds when it saved (AE-18: no dialog)', async () => {
    const dirty = source(true, 'saved')
    expect(await runGuard([dirty])).toEqual({ kind: 'proceed' })
    expect(dirty.flushes).toEqual([FLUSH_TIMEOUT_MS])
  })

  it('refuses without a dialog while offline (AE-19)', async () => {
    expect(await runGuard([source(true, 'offline')])).toEqual({ kind: 'refused-offline' })
  })

  it('asks after a failed flush, and says whether trying again can help', async () => {
    expect(await runGuard([source(true, 'failed')])).toEqual({ kind: 'ask', retryable: false })
    expect(await runGuard([source(true, 'retryable')])).toEqual({ kind: 'ask', retryable: true })
  })

  it('flushes only the dirty sources, and one that did not save makes the whole answer ask', async () => {
    const saved = source(true, 'saved')
    const clean = source(false, 'failed')
    const failed = source(true, 'retryable')
    expect(await runGuard([saved, clean, failed])).toEqual({ kind: 'ask', retryable: true })
    expect(saved.flushes).toHaveLength(1)
    expect(clean.flushes).toHaveLength(0)
    expect(failed.flushes).toHaveLength(1)
  })

  it('offline beats failed: nothing is asked of a GM who cannot save anyway', async () => {
    expect(await runGuard([source(true, 'failed'), source(true, 'offline')])).toEqual({ kind: 'refused-offline' })
  })

  it('a mixed failure is not retryable unless every unsaved source is', async () => {
    expect(await runGuard([source(true, 'retryable'), source(true, 'failed')])).toEqual({
      kind: 'ask',
      retryable: false,
    })
  })

  it('a flush that never answers is cut off at the timeout and may be tried again', async () => {
    vi.useFakeTimers()
    const hung = source(true, 'hang')
    const pending = runGuard([hung])
    await vi.advanceTimersByTimeAsync(FLUSH_TIMEOUT_MS)
    expect(await pending).toEqual({ kind: 'ask', retryable: true })
  })

  it('a flush that throws is a failed save, never a rejection', async () => {
    await expect(runGuard([source(true, 'throw')])).resolves.toEqual({ kind: 'ask', retryable: false })
  })

  it('treats a source that throws from isDirty as dirty rather than losing text', async () => {
    const odd: CanvasDirtySource = {
      isDirty: () => {
        throw new Error('broken')
      },
      flush: () => Promise.resolve('failed'),
      discard: () => {},
    }
    await expect(runGuard([odd])).resolves.toEqual({ kind: 'ask', retryable: false })
  })
})
