import { TableIdentitySchema, type TableIdentity } from '../gm/contracts'
import { isSignedOutResponse } from '../authResponse'
import { tableReadRetryAt } from './readResponse'

export type TableActor = TableIdentity['kind']
export type IdentityResult = { kind: TableActor | 'signed-out' } | { kind: 'unavailable'; retryAt?: number }

/** This endpoint identifies the effective cookie without naming an account, seat or campaign. */
export async function readTableIdentity(request: typeof fetch, signal: AbortSignal): Promise<IdentityResult> {
  try {
    const response = await request('/table/identity', { cache: 'no-store', redirect: 'error', signal })
    if (response.status === 401) {
      return await isSignedOutResponse(response) ? { kind: 'signed-out' } : { kind: 'unavailable' }
    }
    if (response.status === 429) {
      return { kind: 'unavailable', retryAt: tableReadRetryAt(response) }
    }
    if (response.ok) {
      const result = TableIdentitySchema.safeParse(await response.json())
      if (result.success) return { kind: result.data.kind }
    }
  } catch {
    // Offline, interrupted, or an older server returning HTML.
  }
  return { kind: 'unavailable' }
}
