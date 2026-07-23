import * as React from 'react'
import { LocalStorageConversationStore } from './conversationStore'
import type { ConversationStore } from './conversationStore'

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
  const [defaultStore] = React.useState<ConversationStore>(
    () => new LocalStorageConversationStore(),
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
