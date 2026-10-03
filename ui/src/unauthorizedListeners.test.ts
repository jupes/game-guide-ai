/**
 * unauthorizedListeners.test.ts -- `addUnauthorizedListener` (agent-forge-harness-1kg.7.3 PR-2,
 * REVEAL-16): a surface that needs to know the session was lost (to say what the table can still
 * see) listens beside the one centralized handler, and never replaces it.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { addUnauthorizedListener, notifyUnauthorized, setUnauthorizedHandler } from './api'

afterEach(() => setUnauthorizedHandler(null))

describe('addUnauthorizedListener', () => {
  it('runs before the handler, and the handler still runs', () => {
    const order: string[] = []
    setUnauthorizedHandler(() => order.push('handler'))
    const remove = addUnauthorizedListener(() => order.push('listener'))
    notifyUnauthorized()
    expect(order).toEqual(['listener', 'handler'])
    remove()
  })

  it('stops running once removed', () => {
    const listener = vi.fn()
    const remove = addUnauthorizedListener(listener)
    notifyUnauthorized()
    remove()
    notifyUnauthorized()
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('a listener that throws never stops the sign-out', () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    const remove = addUnauthorizedListener(() => {
      throw new Error('boom')
    })
    expect(() => notifyUnauthorized()).not.toThrow()
    expect(handler).toHaveBeenCalledTimes(1)
    remove()
  })
})
