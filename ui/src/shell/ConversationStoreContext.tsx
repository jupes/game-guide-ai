import * as React from 'react'
import { LocalStorageConversationStore } from './conversationStore'
import type { ConversationStore } from './conversationStore'
import { CurrentUserContext } from './currentUser'

const fallbackStore = new LocalStorageConversationStore()

// eslint-disable-next-line react-refresh/only-export-components -- context co-located with provider; HMR-only rule
export const ConversationStoreContext = React.createContext<ConversationStore>(
  fallbackStore,
)

export function ConversationStoreProvider({
  store,
  children,
}: {
  store?: ConversationStore
  children: React.ReactNode
}): React.JSX.Element {
  // Conversations are per-account (x5bz.2): keyed by the signed-in identity so
  // the next person to sign in on this browser can't see the previous user's
  // list. Rebuilt when the identity changes — a plain useState would keep the
  // signed-out store (and its data) across a sign-in.
  // Read the context directly (not useCurrentUser, which throws) so this
  // provider still works when mounted standalone — e.g. component tests that
  // pass an explicit `store`.
  const currentUser = React.useContext(CurrentUserContext)
  const userId = currentUser?.user.id ?? 'guest'
  const defaultStore = React.useMemo(
    () => new LocalStorageConversationStore(userId),
    [userId],
  )

  return (
    <ConversationStoreContext.Provider value={store ?? defaultStore}>
      {children}
    </ConversationStoreContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useConversationStore(): ConversationStore {
  const store = React.useContext(ConversationStoreContext)
  React.useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  )
  return store
}
