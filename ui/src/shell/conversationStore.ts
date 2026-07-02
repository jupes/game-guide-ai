import type { ChatMode } from '../api'

export interface Conversation {
  id: string
  mode: ChatMode
  title: string
  createdAt: string
}

export interface ConversationStore {
  list(mode: ChatMode): Conversation[]
  create(mode: ChatMode, firstPrompt?: string): Conversation
  rename(id: string, title: string): void
  remove(id: string): void
}

export class MemoryConversationStore implements ConversationStore {
  private convs: Conversation[] = []

  list(mode: ChatMode): Conversation[] {
    return this.convs.filter((c) => c.mode === mode)
  }

  create(mode: ChatMode, firstPrompt?: string): Conversation {
    const title = firstPrompt ? firstPrompt.slice(0, 40).trim() : 'New conversation'
    const c: Conversation = {
      id: crypto.randomUUID(),
      mode,
      title,
      createdAt: new Date().toISOString(),
    }
    this.convs = [...this.convs, c]
    return c
  }

  rename(id: string, title: string): void {
    this.convs = this.convs.map((c) => (c.id === id ? { ...c, title } : c))
  }

  remove(id: string): void {
    this.convs = this.convs.filter((c) => c.id !== id)
  }
}

const STORAGE_KEY = 'game-guide-ai:conversations'
// Pre-rename key (project was "rag-chat"). Migrated on first load so existing
// users keep their saved conversations. Safe to remove once no clients hold it.
const LEGACY_STORAGE_KEY = 'rag-chat:conversations'

export class LocalStorageConversationStore implements ConversationStore {
  private load(): Conversation[] {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? this.migrateLegacy() ?? '[]') as Conversation[]
    } catch {
      return []
    }
  }

  // One-time move of conversations stored under the old key onto the new one.
  // Returns the legacy payload (if any) so the caller can parse it immediately.
  private migrateLegacy(): string | null {
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY)
    if (legacy === null) return null
    try {
      localStorage.setItem(STORAGE_KEY, legacy)
      localStorage.removeItem(LEGACY_STORAGE_KEY)
    } catch {
      // Quota/availability errors: still return the payload so this session reads it.
    }
    return legacy
  }

  private save(rows: Conversation[]): void {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(rows))
    } catch (err) {
      // setItem throws when the quota is exceeded (common on mobile) or storage
      // is unavailable (private mode). Surface it as a warning rather than letting
      // it bubble up and crash the create/rename/remove call that triggered it.
      console.warn('[conversationStore] failed to persist conversations to localStorage:', err)
    }
  }

  list(mode: ChatMode): Conversation[] {
    return this.load().filter((c) => c.mode === mode)
  }

  create(mode: ChatMode, firstPrompt?: string): Conversation {
    const title = firstPrompt ? firstPrompt.slice(0, 40).trim() : 'New conversation'
    const c: Conversation = {
      id: crypto.randomUUID(),
      mode,
      title,
      createdAt: new Date().toISOString(),
    }
    this.save([...this.load(), c])
    return c
  }

  rename(id: string, title: string): void {
    this.save(this.load().map((c) => (c.id === id ? { ...c, title } : c)))
  }

  remove(id: string): void {
    this.save(this.load().filter((c) => c.id !== id))
  }
}
