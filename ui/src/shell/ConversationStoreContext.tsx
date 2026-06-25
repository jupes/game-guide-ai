import * as React from 'react'
import { LocalStorageConversationStore } from './conversationStore'
import type { ConversationStore } from './conversationStore'

// eslint-disable-next-line react-refresh/only-export-components -- context co-located with provider; HMR-only rule
export const ConversationStoreContext = React.createContext<ConversationStore>(
  new LocalStorageConversationStore(),
)

export function ConversationStoreProvider({
  store = new LocalStorageConversationStore(),
  children,
}: {
  store?: ConversationStore
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <ConversationStoreContext.Provider value={store}>
      {children}
    </ConversationStoreContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useConversationStore(): ConversationStore {
  return React.useContext(ConversationStoreContext)
}
