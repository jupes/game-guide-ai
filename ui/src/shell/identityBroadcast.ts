/**
 * identityBroadcast -- "the signed-in account may have changed" between the
 * tabs of one browser (SEC-49: sign-out reaches every tab; I-9;
 * agent-forge-harness-1kg.2.5, PR-1a).
 *
 * The message says only that something changed, never who: it carries no
 * email, role or id, so a listener learns nothing it could not learn from its
 * own next request. A receiving tab asks the server (`GET /auth/me`) and acts
 * on that answer alone. A tab never hears its own post (BroadcastChannel
 * semantics), and a listener never re-posts what it received.
 *
 * Feature-detected: without `BroadcastChannel` this is a no-op and a tab learns
 * of another tab's sign-out on its next request (the centralized 401). There is
 * no `storage`-event fallback: that would need a web-storage entry (SEC-49).
 * `1kg.7.4` subscribes the table bundle to the same channel name.
 */

export const IDENTITY_CHANNEL = 'aetheril:identity'

export interface IdentityMessage {
  readonly v: 1
  readonly kind: 'identity-changed'
}

export function identityMessage(): IdentityMessage {
  return { v: 1, kind: 'identity-changed' }
}

/** Exactly `{v: 1, kind: 'identity-changed'}`: a message with any other shape,
 * or any extra key, is not ours and is ignored. */
export function isIdentityMessage(data: unknown): data is IdentityMessage {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return false
  const keys = Object.keys(data)
  if (keys.length !== 2 || !Object.hasOwn(data, 'v') || !Object.hasOwn(data, 'kind')) return false
  const { v, kind } = data as Record<string, unknown>
  return v === 1 && kind === 'identity-changed'
}

/** The slice of `BroadcastChannel` this module uses, so a test can inject a
 * fake rather than Node's global (which delivers across tests in one worker and
 * keeps the process alive until closed). */
export interface IdentityChannelLike {
  postMessage(message: unknown): void
  close(): void
  onmessage: ((event: MessageEvent) => void) | null
  /** Node's channel has it and a browser's does not: see `defaultChannelFactory`. */
  unref?: () => void
}

export type IdentityChannelFactory = (name: string) => IdentityChannelLike | null

/** A real channel, or `null` where the platform has none. Under Node (vitest's
 * jsdom project) the channel is `unref()`ed, so a provider a test forgot to
 * unmount cannot keep the run alive. */
export const defaultChannelFactory: IdentityChannelFactory = (name) => {
  if (typeof BroadcastChannel !== 'function') return null
  const channel: IdentityChannelLike = new BroadcastChannel(name)
  if (typeof channel.unref === 'function') channel.unref()
  return channel
}

export interface IdentityBroadcast {
  /** Tell the other tabs that the identity may have changed. */
  post(): void
  /** Stop listening and release the channel. Safe to call twice. */
  close(): void
}

/** Open the channel -- lazily, when a provider mounts, never at import -- and
 * call `onSignal` for each well-formed message from another tab. */
export function openIdentityBroadcast(
  onSignal: () => void,
  factory: IdentityChannelFactory = defaultChannelFactory,
): IdentityBroadcast {
  let channel = factory(IDENTITY_CHANNEL)
  if (channel !== null) {
    channel.onmessage = (event) => {
      if (isIdentityMessage(event.data)) onSignal()
    }
  }
  return {
    post() {
      try {
        channel?.postMessage(identityMessage())
      } catch {
        // A channel closed under us (an unmount racing a sign-out): the other
        // tabs will learn on their next request, as without a channel at all.
      }
    },
    close() {
      if (channel === null) return
      channel.onmessage = null
      channel.close()
      channel = null
    },
  }
}
