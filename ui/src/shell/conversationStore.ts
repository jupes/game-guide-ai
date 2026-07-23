import type { ChatMode } from '../api'

export interface Conversation {
  id: string
  mode: ChatMode
  title: string
  derivedTitle: string
  customTitle: string | null
  hasFirstPrompt: boolean
  createdAt: string
}

const NEW_CONVERSATION_TITLE = 'New conversation'

export function deriveConversationTitle(prompt: string): string {
  return prompt.slice(0, 40).trim() || NEW_CONVERSATION_TITLE
}

function isChatMode(value: unknown): value is ChatMode {
  return value === 'sage' || value === 'spell' || value === 'rules' || value === 'gm'
}

function normalizeConversation(value: unknown): Conversation | null {
  if (typeof value !== 'object' || value === null) return null
  const row = value as Record<string, unknown>
  if (
    typeof row.id !== 'string'
    || !isChatMode(row.mode)
    || typeof row.title !== 'string'
    || typeof row.createdAt !== 'string'
  ) {
    return null
  }

  const legacyTitle = row.title.trim() || NEW_CONVERSATION_TITLE
  const derivedTitle =
    typeof row.derivedTitle === 'string'
      ? row.derivedTitle.trim() || NEW_CONVERSATION_TITLE
      : legacyTitle
  const customTitle =
    typeof row.customTitle === 'string' && row.customTitle.trim()
      ? row.customTitle.trim()
      : null
  const hasFirstPrompt =
    typeof row.hasFirstPrompt === 'boolean'
      ? row.hasFirstPrompt
      : legacyTitle !== NEW_CONVERSATION_TITLE

  return {
    id: row.id,
    mode: row.mode,
    title: customTitle ?? derivedTitle,
    derivedTitle,
    customTitle,
    hasFirstPrompt,
    createdAt: row.createdAt,
  }
}

function createConversation(mode: ChatMode, firstPrompt?: string): Conversation {
  const derivedTitle = deriveConversationTitle(firstPrompt ?? '')
  return {
    id: crypto.randomUUID(),
    mode,
    title: derivedTitle,
    derivedTitle,
    customTitle: null,
    hasFirstPrompt: Boolean(firstPrompt?.trim()),
    createdAt: new Date().toISOString(),
  }
}

function updateConversation(
  rows: Conversation[],
  id: string,
  update: (conversation: Conversation) => Conversation,
): Conversation[] | null {
  const index = rows.findIndex((conversation) => conversation.id === id)
  if (index === -1) return null
  const current = rows[index]
  const updated = update(current)
  if (updated === current) return null
  const next = [...rows]
  next[index] = updated
  return next
}

function recordFirstPrompt(
  rows: Conversation[],
  id: string,
  prompt: string,
): Conversation[] | null {
  if (!prompt.trim()) return null
  return updateConversation(rows, id, (conversation) => {
    if (conversation.hasFirstPrompt) return conversation
    const derivedTitle = deriveConversationTitle(prompt)
    return {
      ...conversation,
      title: conversation.customTitle ?? derivedTitle,
      derivedTitle,
      hasFirstPrompt: true,
    }
  })
}

function renameConversation(
  rows: Conversation[],
  id: string,
  title: string,
): Conversation[] | null {
  const customTitle = title.trim() || null
  return updateConversation(rows, id, (conversation) => ({
    ...conversation,
    title: customTitle ?? conversation.derivedTitle,
    customTitle,
  }))
}

export interface ConversationStore {
  list(mode: ChatMode): Conversation[]
  get(id: string): Conversation | undefined
  create(mode: ChatMode, firstPrompt?: string): Conversation
  recordFirstPrompt(id: string, prompt: string): void
  rename(id: string, title: string): void
  remove(id: string): void
  subscribe(listener: () => void): () => void
  getSnapshot(): number
}

abstract class ObservableConversationStore {
  private revision = 0
  private readonly listeners = new Set<() => void>()

  readonly getSnapshot = (): number => this.revision

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  protected notifyChanged(): void {
    this.revision += 1
    this.listeners.forEach((listener) => listener())
  }
}

export class MemoryConversationStore
  extends ObservableConversationStore
  implements ConversationStore {
  private convs: Conversation[] = []

  list(mode: ChatMode): Conversation[] {
    return this.convs.filter((c) => c.mode === mode)
  }

  create(mode: ChatMode, firstPrompt?: string): Conversation {
    const conversation = createConversation(mode, firstPrompt)
    this.convs = [...this.convs, conversation]
    this.notifyChanged()
    return conversation
  }

  get(id: string): Conversation | undefined {
    return this.convs.find((c) => c.id === id)
  }

  recordFirstPrompt(id: string, prompt: string): void {
    const next = recordFirstPrompt(this.convs, id, prompt)
    if (next === null) return
    this.convs = next
    this.notifyChanged()
  }

  rename(id: string, title: string): void {
    const next = renameConversation(this.convs, id, title)
    if (next === null) return
    this.convs = next
    this.notifyChanged()
  }

  remove(id: string): void {
    const next = this.convs.filter((c) => c.id !== id)
    if (next.length === this.convs.length) return
    this.convs = next
    this.notifyChanged()
  }
}

const STORAGE_KEY = 'game-guide-ai:conversations'
// Pre-rename key (project was "rag-chat"). Migrated on first load so existing
// users keep their saved conversations. Safe to remove once no clients hold it.
const LEGACY_STORAGE_KEY = 'rag-chat:conversations'

export class LocalStorageConversationStore
  extends ObservableConversationStore
  implements ConversationStore {
  private load(): Conversation[] {
    try {
      const parsed: unknown = JSON.parse(
        localStorage.getItem(STORAGE_KEY) ?? this.migrateLegacy() ?? '[]',
      )
      if (!Array.isArray(parsed)) return []
      return parsed
        .map(normalizeConversation)
        .filter((row): row is Conversation => row !== null)
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

  private save(rows: Conversation[]): boolean {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(rows))
      return true
    } catch (err) {
      // setItem throws when the quota is exceeded (common on mobile) or storage
      // is unavailable (private mode). Surface it as a warning rather than letting
      // it bubble up and crash the create/rename/remove call that triggered it.
      console.warn('[conversationStore] failed to persist conversations to localStorage:', err)
      return false
    }
  }

  list(mode: ChatMode): Conversation[] {
    return this.load().filter((c) => c.mode === mode)
  }

  get(id: string): Conversation | undefined {
    return this.load().find((c) => c.id === id)
  }

  create(mode: ChatMode, firstPrompt?: string): Conversation {
    const conversation = createConversation(mode, firstPrompt)
    if (this.save([...this.load(), conversation])) this.notifyChanged()
    return conversation
  }

  recordFirstPrompt(id: string, prompt: string): void {
    const next = recordFirstPrompt(this.load(), id, prompt)
    if (next !== null && this.save(next)) this.notifyChanged()
  }

  rename(id: string, title: string): void {
    const next = renameConversation(this.load(), id, title)
    if (next !== null && this.save(next)) this.notifyChanged()
  }

  remove(id: string): void {
    const rows = this.load()
    const next = rows.filter((c) => c.id !== id)
    if (next.length !== rows.length && this.save(next)) this.notifyChanged()
  }
}
