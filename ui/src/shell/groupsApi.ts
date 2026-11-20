import { notifyUnauthorized } from '../api'
import {
  CONTRACT_VERSION, ErrorBodySchema, GroupCreateRequestSchema, GroupPageSchema, GroupPatchRequestSchema,
  GroupSchema, type Group,
} from '../gm/contracts'
import { isOpaqueId } from './workspaceFragment'

export type GroupChange =
  | { kind: 'create'; name: string; commandId: string }
  | { kind: 'rename'; name: string; groupId: string }
  | { kind: 'remove'; groupId: string }
  | { kind: 'add-member' | 'remove-member'; groupId: string; seatId: string }

type GroupFailure = { kind: 'invalid' | 'unavailable' | 'failed' | 'duplicate' | 'limit' | 'throttled' }

async function request(fetchImpl: typeof fetch, url: string, method = 'GET', body: unknown = {}) {
  try {
    return await fetchImpl(url, {
      credentials: 'include',
      ...(method === 'GET' ? {} : {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }),
    })
  } catch {
    return null
  }
}

async function dataOf(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return undefined
  }
}

function groupPath(campaignId: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/groups`
}

export async function readGroups(
  campaignId: string, fetchImpl: typeof fetch = fetch,
): Promise<readonly Group[] | null> {
  if (!isOpaqueId(campaignId)) return null
  const response = await request(fetchImpl, groupPath(campaignId))
  if (response?.status === 401) notifyUnauthorized()
  if (!response?.ok) return null
  const page = GroupPageSchema.safeParse(await dataOf(response))
  return page.success && page.data.next_cursor === null ? page.data.items : null
}

export async function writeGroup(
  campaignId: string, change: GroupChange, fetchImpl: typeof fetch = fetch,
): Promise<{ kind: 'ok'; group: Group | null } | GroupFailure> {
  if (!isOpaqueId(campaignId) || ('groupId' in change && !isOpaqueId(change.groupId))
    || ('seatId' in change && !isOpaqueId(change.seatId))) return { kind: 'unavailable' }
  const base = groupPath(campaignId)
  const target = 'groupId' in change ? `${base}/${encodeURIComponent(change.groupId)}` : base
  let url = target
  let body: object = {}
  switch (change.kind) {
    case 'create': {
      const parsed = GroupCreateRequestSchema.safeParse({
        schema_version: CONTRACT_VERSION, command_id: change.commandId, name: change.name,
      })
      if (!parsed.success) return { kind: 'invalid' }
      body = parsed.data
      break
    }
    case 'rename': {
      const parsed = GroupPatchRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, name: change.name })
      if (!parsed.success) return { kind: 'invalid' }
      body = parsed.data
      break
    }
    case 'remove':
      url = `${target}/remove`
      break
    case 'add-member':
    case 'remove-member':
      url = `${target}/members/${encodeURIComponent(change.seatId)}${change.kind === 'remove-member' ? '/remove' : ''}`
      break
  }
  const response = await request(fetchImpl, url, change.kind === 'rename' ? 'PATCH' : 'POST', body)
  if (response === null) return { kind: 'failed' }
  if (response.status === 401) notifyUnauthorized()
  if ([401, 403, 404].includes(response.status)) return { kind: 'unavailable' }
  if (response.status === 429) return { kind: 'throttled' }
  if (response.status === 422) return { kind: 'invalid' }
  if (response.status === 409) {
    const error = ErrorBodySchema.safeParse(await dataOf(response))
    if (error.success && error.data.detail.code === 'group_name_taken') return { kind: 'duplicate' }
    if (error.success && error.data.detail.code === 'group_cap_reached') return { kind: 'limit' }
  }
  if (!response.ok) return { kind: 'failed' }
  if (change.kind !== 'create' && change.kind !== 'rename') return { kind: 'ok', group: null }
  const parsed = GroupSchema.safeParse(await dataOf(response))
  if (!parsed.success || (change.kind === 'rename' && parsed.data.group_id !== change.groupId)) return { kind: 'failed' }
  return { kind: 'ok', group: parsed.data }
}
