import * as React from 'react'
import { FALLBACK_CATALOG } from './modelPreference'
import type { ModelCatalog } from './modelPreference'

type CatalogState = readonly [ModelCatalog, (catalog: ModelCatalog) => void]

const ModelCatalogContext = React.createContext<CatalogState | null>(null)

/**
 * agent-forge-harness-bta: holds the /models catalog ModelPicker loads so that
 * ChatPane reads the SAME one — what the picker shows is what the pane sends.
 * WorkspaceShell mounts it around both. The picker still does the fetch (its
 * `getModels` stays injectable); this only hoists where the answer lives.
 */
export function ModelCatalogProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [catalog, setCatalog] = React.useState<ModelCatalog>(FALLBACK_CATALOG)
  const value = React.useMemo<CatalogState>(() => [catalog, setCatalog], [catalog])
  return <ModelCatalogContext.Provider value={value}>{children}</ModelCatalogContext.Provider>
}

/**
 * The shared catalog and its setter, or — with no provider above — this
 * component's own, which starts (and, for a reader that never loads one,
 * stays) at the offline fallback. A pane mounted without the picker therefore
 * sends only what the fallback allows: 'auto', or a started conversation's
 * bound preference.
 */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useModelCatalogState(): CatalogState {
  const shared = React.useContext(ModelCatalogContext)
  const [catalog, setCatalog] = React.useState<ModelCatalog>(FALLBACK_CATALOG)
  const own = React.useMemo<CatalogState>(() => [catalog, setCatalog], [catalog])
  return shared ?? own
}
