/**
 * lossGuard -- the pure core of the Workbench's loss guard (agent-forge-harness-1kg.6.3;
 * interactions ADR §5.3, CANVAS-16, CANVAS-17).
 *
 * Because saving is automatic, most dirty state is merely un-flushed, so the guard
 * first FLUSHES (waiting at most `FLUSH_TIMEOUT_MS`) and asks only when that does
 * not work:
 *
 * - nothing registered, or nothing dirty: proceed, no dialog;
 * - every dirty source saved: proceed, no dialog (AE-18);
 * - any source offline: refuse, no dialog, say why (AE-19). A GM who cannot save
 *   is never asked whether to throw text away;
 * - otherwise ask (`LossGuardDialog`), and say whether trying again can help.
 *
 * Nothing here touches the DOM or the network, and nothing here throws or rejects:
 * a flush that throws, or never answers, is a failed save. The caller (the canvas
 * provider) owns the dialog and the announcement.
 *
 * PR-1 registers no dirty source: the canvas is read-only until 1kg.6.5, so
 * production never reaches the dialog yet. The contract is proven with test sources.
 */

export type FlushOutcome = 'saved' | 'failed' | 'retryable' | 'offline'

/** One thing that can hold unsaved text. 1kg.6.5's field editors register one. */
export interface CanvasDirtySource {
  isDirty(): boolean
  /** Try to save now, answering within `timeoutMs`. */
  flush(timeoutMs: number): Promise<FlushOutcome>
  /** Throw the unsaved text away (the destructive button, and only that). */
  discard(): void
}

export const FLUSH_TIMEOUT_MS = 5000

export type GuardStep = 'proceed' | 'refuse-offline' | 'ask'

/** Pure: what the guard does after one flush. */
export function guardStep(outcome: FlushOutcome): GuardStep {
  if (outcome === 'saved') return 'proceed'
  if (outcome === 'offline') return 'refuse-offline'
  return 'ask'
}

export type GuardResult =
  | { readonly kind: 'proceed' }
  | { readonly kind: 'refused-offline' }
  | { readonly kind: 'ask'; readonly retryable: boolean }

function dirtyOf(source: CanvasDirtySource): boolean {
  try {
    return source.isDirty()
  } catch {
    // Unknown is treated as dirty: the safe direction loses nothing.
    return true
  }
}

async function flushed(source: CanvasDirtySource): Promise<FlushOutcome> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const timedOut = new Promise<FlushOutcome>((resolve) => {
    timer = setTimeout(() => resolve('retryable'), FLUSH_TIMEOUT_MS)
  })
  try {
    return await Promise.race([source.flush(FLUSH_TIMEOUT_MS), timedOut])
  } catch {
    return 'failed'
  } finally {
    clearTimeout(timer)
  }
}

/** Flush every dirty source and decide. Never rejects. */
export async function runGuard(sources: readonly CanvasDirtySource[]): Promise<GuardResult> {
  const dirty = sources.filter(dirtyOf)
  if (dirty.length === 0) return { kind: 'proceed' }
  const outcomes = await Promise.all(dirty.map(flushed))
  const steps = outcomes.map(guardStep)
  if (steps.includes('refuse-offline')) return { kind: 'refused-offline' }
  const unsaved = outcomes.filter((outcome) => outcome !== 'saved')
  if (unsaved.length === 0) return { kind: 'proceed' }
  return { kind: 'ask', retryable: unsaved.every((outcome) => outcome === 'retryable') }
}
