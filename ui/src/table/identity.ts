import { TableIdentitySchema, type TableIdentity } from '../gm/contracts'

export type TableActor = TableIdentity['kind']
export type IdentityResult = { kind: TableActor | 'signed-out' } | { kind: 'unavailable'; retryAt?: number }

export async function isSignedOutResponse(response: Response): Promise<boolean> {
  if (response.status !== 401 || response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') return false
  try {
    const body = await response.json()
    return body?.detail === 'not signed in' && Object.keys(body).length === 1
  } catch {
    return false
  }
}

/** This endpoint identifies the effective cookie without naming an account, seat or campaign. */
export async function readTableIdentity(request: typeof fetch, signal: AbortSignal): Promise<IdentityResult> {
  try {
    const response = await request('/table/identity', { cache: 'no-store', redirect: 'error', signal })
    if (response.status === 401) {
      return await isSignedOutResponse(response) ? { kind: 'signed-out' } : { kind: 'unavailable' }
    }
    if (response.status === 429) {
      const header = response.headers.get('Retry-After')?.trim()
      const now = Date.now()
      const until = header && /^\d+$/.test(header)
        ? Math.min(Number.MAX_SAFE_INTEGER, now + Number(header) * 1000) : Date.parse(header ?? '')
      return { kind: 'unavailable', retryAt: Number.isFinite(until) ? Math.max(now + 1000, until) : now + 5000 }
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
