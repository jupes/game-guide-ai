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

  // a6o: a preference the served catalog does not list (a model alias stored
  // before D-9, or a retired id) goes back to the default — but only BEFORE
  // the first prompt. Once a conversation has sent one, it is already bound
  // server-side (D6's Conversation affinity) to whatever it last posted, alias
  // included (agent-forge-harness-a6o/#122's pre-D9 exception in
  // service/app.py). agent-forge-harness-bta wires this stored value straight
  // into every subsequent `/chat` call, so rewriting it here after that point
  // would desync the client from the server's own binding and turn the very
  // next turn into a 409 (claim_conversation_strategy mismatch). Only the
  // served catalog can tell a stale value apart from a current one: the
  // offline fallback lists 'auto' alone, and an alias list here would name
  // the models in the bundle.
  React.useEffect(() => {
    if (catalog === FALLBACK_CATALOG || conversationId === null) return
    if (conversation?.hasFirstPrompt) return
    if (catalog.models.some((m) => m.id === value)) return
    store.setModelPreference(conversationId, catalog.default)
  }, [catalog, conversation?.hasFirstPrompt, conversationId, store, value])

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
