import { z } from 'zod'
import { notifyUnauthorized } from '../api'
import {
  parseToolInvocation,
  readErrorBody,
  TOOL_IDS,
  type ErrorInfo,
  type ToolId,
  type ToolInvocation,
  type ToolInvocationRequest,
} from './contracts'
import type { ToolAvailability } from './registry'

export type InvocationReply =
  | { kind: 'ok'; invocation: ToolInvocation }
  | { kind: 'refused'; error: ErrorInfo }
  | { kind: 'lost' }
  | { kind: 'unauthorized' }

const AvailabilitySchema = z.record(z.string(), z.string().min(1).max(500).nullable())

function campaignPath(campaignId: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}`
}

async function send(path: string, fetchImpl: typeof fetch, body?: object): Promise<Response | null> {
  try {
    if (body === undefined) return await fetchImpl(path, { credentials: 'include' })
    return await fetchImpl(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    })
  } catch {
    return null
  }
}

async function read(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return undefined
  }
}

export async function loadToolAvailability(
  campaignId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Record<ToolId, ToolAvailability> | null> {
  const response = await send(`${campaignPath(campaignId)}/tool-availability`, fetchImpl)
  if (response?.status === 401) notifyUnauthorized()
  if (!response?.ok) return null
  const parsed = AvailabilitySchema.safeParse(await read(response))
  if (!parsed.success || !TOOL_IDS.every((tool) => Object.hasOwn(parsed.data, tool))) return null
  const entries = TOOL_IDS.map((tool) => {
    const reason = parsed.data[tool] ?? null
    return [tool, { enabled: reason === null, reason }] as const
  })
  return Object.fromEntries(entries) as Record<ToolId, ToolAvailability>
}

async function reply(
  response: Response | null,
  id: string,
  tool: ToolId | undefined,
): Promise<InvocationReply> {
  if (response === null) return { kind: 'lost' }
  if (response.status === 401) {
    notifyUnauthorized()
    return { kind: 'unauthorized' }
  }
  const raw = await read(response)
  if (response.ok) {
    const parsed = parseToolInvocation(raw)
    const entryId = response.headers.get('x-workbench-entry-id')
    return parsed.kind === 'ok' && parsed.value.invocation_id === id
      && (tool === undefined || parsed.value.tool_id === tool)
      ? {
          kind: 'ok',
          invocation: {
            ...parsed.value,
            ...(entryId !== null && /^[A-Za-z0-9_-]{1,64}$/.test(entryId) ? { entry_id: entryId } : {}),
          },
        }
      : { kind: 'lost' }
  }
  const error = readErrorBody(raw)
  if (error.kind === 'workbench') return { kind: 'refused', error: error.info }
  // A gateway can drop the response after admission. Check by id before
  // offering another attempt.
  if (response.status >= 500) return { kind: 'lost' }
  if (response.status === 429) {
    return { kind: 'refused', error: { code: 'tool_throttled', message: 'The tool is busy. Try again shortly.', retryable: true } }
  }
  return {
    kind: 'refused',
    error: { code: 'tool_unavailable', message: "Couldn't access the tool run.", retryable: false },
  }
}

export async function startTool(
  request: ToolInvocationRequest,
  fetchImpl: typeof fetch = fetch,
): Promise<InvocationReply> {
  const response = await send(`${campaignPath(request.campaign_id)}/tool-invocations`, fetchImpl, request)
  return reply(response, request.invocation_id, request.tool_id)
}

export async function checkTool(
  campaignId: string,
  id: string,
  fetchImpl: typeof fetch = fetch,
): Promise<InvocationReply> {
  const response = await send(`${campaignPath(campaignId)}/tool-invocations/${encodeURIComponent(id)}`, fetchImpl)
  return reply(response, id, undefined)
}

export async function cancelTool(
  campaignId: string,
  id: string,
  fetchImpl: typeof fetch = fetch,
): Promise<InvocationReply> {
  const response = await send(`${campaignPath(campaignId)}/tool-invocations/${encodeURIComponent(id)}/cancel`, fetchImpl, {})
  return reply(response, id, undefined)
}
