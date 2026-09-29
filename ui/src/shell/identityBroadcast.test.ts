/**
 * identityBroadcast.test.ts -- the cross-tab identity signal
 * (agent-forge-harness-1kg.2.5, PR-1a; I-9, critic items 12(e), 13).
 *
 * Never uses Node's global BroadcastChannel directly (brief F-19): a fake
 * channel is injected, and the default factory is exercised against a stub.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  IDENTITY_CHANNEL,
  defaultChannelFactory,
  isIdentityMessage,
  openIdentityBroadcast,
  type IdentityChannelLike,
} from './identityBroadcast'

class FakeChannel implements IdentityChannelLike {
  readonly posted: unknown[] = []
  closed = 0
  onmessage: ((event: MessageEvent) => void) | null = null
  readonly name: string
  constructor(name: string) {
    this.name = name
  }
  postMessage(message: unknown): void {
    if (this.closed > 0) throw new DOMException('closed', 'InvalidStateError')
    this.posted.push(message)
  }
  close(): void {
    this.closed += 1
  }
  deliver(data: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data }))
  }
}

function fakeFactory(): { factory: (name: string) => FakeChannel; opened: FakeChannel[] } {
  const opened: FakeChannel[] = []
  return {
    opened,
    factory: (name) => {
      const channel = new FakeChannel(name)
      opened.push(channel)
      return channel
    },
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the message', () => {
  it('posts exactly {v: 1, kind: "identity-changed"} on the named channel, and nothing else', () => {
    const { factory, opened } = fakeFactory()
    const broadcast = openIdentityBroadcast(() => {}, factory)
    broadcast.post()
    broadcast.post()

    expect(opened).toHaveLength(1)
    expect(opened[0].name).toBe(IDENTITY_CHANNEL)
    expect(opened[0].posted).toHaveLength(2)
    for (const message of opened[0].posted) {
      expect(message).toStrictEqual({ v: 1, kind: 'identity-changed' })
    }
  })

  it.each([
    ['the message', { v: 1, kind: 'identity-changed' }, true],
    ['an extra key (an identity riding along)', { v: 1, kind: 'identity-changed', email: 'ada@example.com' }, false],
    ['another version', { v: 2, kind: 'identity-changed' }, false],
    ['another kind', { v: 1, kind: 'signed-out' }, false],
    ['a string', 'identity-changed', false],
    ['an array', [1, 'identity-changed'], false],
    ['null', null, false],
  ])('recognises %s: %j -> %s', (_label, data, expected) => {
    expect(isIdentityMessage(data)).toBe(expected)
  })
})

describe('receiving', () => {
  it('calls onSignal for a well-formed message only', () => {
    const { factory, opened } = fakeFactory()
    const onSignal = vi.fn()
    openIdentityBroadcast(onSignal, factory)

    opened[0].deliver({ v: 1, kind: 'identity-changed', who: 'ada' })
    opened[0].deliver('identity-changed')
    expect(onSignal).not.toHaveBeenCalled()
    opened[0].deliver({ v: 1, kind: 'identity-changed' })
    expect(onSignal).toHaveBeenCalledTimes(1)
  })

  it('close() stops listening, closes the channel once, and makes post() a no-op', () => {
    const { factory, opened } = fakeFactory()
    const onSignal = vi.fn()
    const broadcast = openIdentityBroadcast(onSignal, factory)

    broadcast.close()
    broadcast.close()
    opened[0].deliver({ v: 1, kind: 'identity-changed' })
    expect(() => broadcast.post()).not.toThrow()

    expect(opened[0].closed).toBe(1)
    expect(opened[0].posted).toHaveLength(0)
    expect(onSignal).not.toHaveBeenCalled()
  })

  it('a post on a channel that is already closed does not throw', () => {
    const channel = new FakeChannel(IDENTITY_CHANNEL)
    const broadcast = openIdentityBroadcast(() => {}, () => channel)
    channel.close()
    expect(() => broadcast.post()).not.toThrow()
  })

  it('without a channel, post and close are no-ops', () => {
    const broadcast = openIdentityBroadcast(() => {}, () => null)
    expect(() => {
      broadcast.post()
      broadcast.close()
    }).not.toThrow()
  })
})

describe('defaultChannelFactory', () => {
  it('opens nothing where the platform has no BroadcastChannel', () => {
    vi.stubGlobal('BroadcastChannel', undefined)
    expect(defaultChannelFactory(IDENTITY_CHANNEL)).toBeNull()
  })

  it('opens a channel by name and unrefs it when the platform can (Node), so it never holds the run open', () => {
    const made: { name: string; unref: ReturnType<typeof vi.fn> }[] = []
    vi.stubGlobal('BroadcastChannel', class {
      unref = vi.fn()
      onmessage = null
      readonly name: string
      constructor(name: string) {
        this.name = name
        made.push(this)
      }
      postMessage(): void {}
      close(): void {}
    })

    const channel = defaultChannelFactory(IDENTITY_CHANNEL)

    expect(channel).not.toBeNull()
    expect(made).toHaveLength(1)
    expect(made[0].name).toBe(IDENTITY_CHANNEL)
    expect(made[0].unref).toHaveBeenCalledTimes(1)
  })

  it('does not require unref (a browser channel has none)', () => {
    vi.stubGlobal('BroadcastChannel', class {
      onmessage = null
      postMessage(): void {}
      close(): void {}
    })
    expect(defaultChannelFactory(IDENTITY_CHANNEL)).not.toBeNull()
  })
})
