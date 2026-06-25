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

const STORAGE_KEY = 'rag-chat:conversations'

export class LocalStorageConversationStore implements ConversationStore {
  private load(): Conversation[] {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]') as Conversation[]
    } catch {
      return []
    }
  }

  private save(rows: Conversation[]): void {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(rows))
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
