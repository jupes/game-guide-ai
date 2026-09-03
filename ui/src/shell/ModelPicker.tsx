/**
 * ModelPicker — per-conversation model preference selector (b8o.2).
 *
 * Lives in AppHeader beside the theme control. Editable freely before the
 * first prompt (the conversation's strategy isn't bound server-side yet);
 * after that, changing it starts a NEW conversation instead of mutating the
 * current one — the plan's "Conversation affinity": a started conversation's
 * routing strategy is atomically bound on its first request and a later
 * change would either silently diverge from what the server already
 * committed to, or (with the /chat 409 guard, b8o.2 slice 2-3) simply fail.
 * Disabled with no active conversation — nothing to bind a preference to yet
 * (mirrors ChatPane's own attachment-button gating on conversationId).
 */

import * as React from 'react'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'
import './ModelPicker.css'

export interface ModelCatalogEntry {
  id: string
  display_name: string
  tier?: string
  supports_attachments?: boolean
  description?: string
}

interface ModelCatalog {
  default: string
  models: ModelCatalogEntry[]
}

export type GetModelsFn = () => Promise<ModelCatalog>

const FALLBACK_CATALOG: ModelCatalog = {
  default: 'auto',
  models: [{ id: 'auto', display_name: 'Automatic' }],
}

async function defaultGetModels(): Promise<ModelCatalog> {
  const res = await fetch('/models', { credentials: 'include' })
  if (!res.ok) return FALLBACK_CATALOG
  try {
    return (await res.json()) as ModelCatalog
  } catch {
    return FALLBACK_CATALOG
  }
}

export interface ModelPickerProps {
  getModels?: GetModelsFn
  /** Injectable for tests; defaults to the browser's window.confirm. */
  confirmChange?: (message: string) => boolean
}

export function ModelPicker({
  getModels = defaultGetModels,
  confirmChange = (message: string) => window.confirm(message),
}: ModelPickerProps): React.JSX.Element {
  const { mode, conversationId, setConversationId } = useAppNav()
  const store = useConversationStore()
  const [catalog, setCatalog] = React.useState<ModelCatalog>(FALLBACK_CATALOG)

  React.useEffect(() => {
    let cancelled = false
    getModels()
      .then((body) => {
        if (!cancelled) setCatalog(body)
      })
      .catch(() => {
        if (!cancelled) setCatalog(FALLBACK_CATALOG)
      })
    return () => {
      cancelled = true
    }
  }, [getModels])

  const conversation = conversationId !== null ? store.get(conversationId) : undefined
  const value = conversation?.modelPreference ?? catalog.default

  const handleChange = (next: string): void => {
    if (conversationId === null || conversation === undefined) return
    if (!conversation.hasFirstPrompt) {
      store.setModelPreference(conversationId, next)
      return
    }
    const label = catalog.models.find((m) => m.id === next)?.display_name ?? next
    if (!confirmChange(`Start a new conversation with ${label}?`)) return
    const fresh = store.create(mode, undefined, next)
    setConversationId(fresh.id)
  }

  return (
    <label className="model-picker">
      <span className="model-picker__label">Model</span>
      <select
        className="model-picker__select"
        value={value}
        disabled={conversationId === null}
        onChange={(e) => handleChange(e.target.value)}
        aria-label="Model"
      >
        {catalog.models.map((m) => (
          <option key={m.id} value={m.id}>{m.display_name}</option>
        ))}
      </select>
    </label>
  )
}
