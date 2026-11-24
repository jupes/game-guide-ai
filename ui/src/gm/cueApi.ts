import { notifyUnauthorized } from '../api'
import {
  AUDIO_MAX_BYTES, AssetCreateRequestSchema, AssetSchema, AttachedCueEntrySchema, CONTRACT_VERSION,
  CueAttachRequestSchema, CueCreateRequestSchema,
  CuePageSchema, CueSchema, ErrorBodySchema, ONE_SHOT_MAX_MS, type Asset, type AttachedCueEntry, type Cue, type CueKind,
  type CueListQuery, type CuePage,
} from './contracts'
import { assetPath } from './documentFields'
import { deleteArchivedResource, type DeleteResult } from './documentApi'

const ONE_SHOT_MAX_BYTES = 5_000_000
export const CUE_REMOVED_EVENT = 'workbench:cue-removed'
const AUDIO_TYPE: Readonly<Record<string, string>> = {
  mp3: 'audio/mpeg', m4a: 'audio/mp4', ogg: 'audio/ogg', wav: 'audio/wav',
}

export class CueApiError extends Error {}

function path(campaign: string, suffix = ''): string {
  return `/campaigns/${encodeURIComponent(campaign)}/cues${suffix}`
}

async function request(
  fetchImpl: typeof fetch, url: string, init: RequestInit, failure: string,
): Promise<Response> {
  let reply: Response
  try {
    reply = await fetchImpl(url, { ...init, credentials: 'include', cache: 'no-store' })
  } catch {
    throw new CueApiError(failure)
  }
  if (reply.status === 401) notifyUnauthorized()
  return reply
}

async function body(reply: Response): Promise<unknown> {
  try {
    return await reply.json()
  } catch {
    return null
  }
}

function json(method: string, data: unknown, signal?: AbortSignal): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), signal }
}

export async function cueCapability(campaign: string, signal: AbortSignal, fetchImpl: typeof fetch): Promise<boolean> {
  try {
    return (await request(fetchImpl, path(campaign, '/capability'), { signal }, '')).status === 204
  } catch {
    return false
  }
}

export async function listCues(query: CueListQuery, signal: AbortSignal, fetchImpl: typeof fetch): Promise<CuePage> {
  const failure = "Couldn't load cues."
  const reply = await request(fetchImpl, path(query.campaign_id, '/list'), json('POST', query, signal), failure)
  const page = CuePageSchema.safeParse(await body(reply))
  if (!reply.ok || !page.success || page.data.campaign_id !== query.campaign_id ||
      page.data.items.some((cue) => cue.campaign_id !== query.campaign_id || cue.archived !== query.archived)) {
    throw new CueApiError(failure)
  }
  return page.data
}

async function checkedCue(reply: Response, campaign: string, failure: string): Promise<Cue> {
  const parsed = CueSchema.safeParse(await body(reply))
  if (!reply.ok || !parsed.success || parsed.data.campaign_id !== campaign) throw new CueApiError(failure)
  return parsed.data
}

export async function updateCue(
  cue: Cue, action: { title: string } | 'archive' | 'restore', fetchImpl: typeof fetch,
): Promise<Cue> {
  const suffix = `/${encodeURIComponent(cue.cue_id)}`
  const failure = "Couldn't save the change. Try again."
  const reply = typeof action === 'string'
    ? await request(fetchImpl, path(cue.campaign_id, `${suffix}/${action}`), { method: 'POST' }, failure)
    : await request(fetchImpl, path(cue.campaign_id, suffix), json('PATCH', {
        schema_version: CONTRACT_VERSION, title: action.title,
      }), failure)
  const saved = await checkedCue(reply, cue.campaign_id, failure)
  if (saved.cue_id !== cue.cue_id) throw new CueApiError(failure)
  return saved
}

export async function deleteCue(cue: Cue, password: string, fetchImpl: typeof fetch): Promise<DeleteResult> {
  const result = await deleteArchivedResource(
    path(cue.campaign_id, `/${encodeURIComponent(cue.cue_id)}/delete`), password, fetchImpl, 'conflict',
  )
  if (result.kind === 'ok') window.dispatchEvent(new CustomEvent(CUE_REMOVED_EVENT, { detail: cue.cue_id }))
  return result
}

export async function readCue(
  campaign: string, cueId: string, signal: AbortSignal, fetchImpl: typeof fetch,
): Promise<Cue> {
  const failure = 'This audio cue is unavailable.'
  const reply = await request(fetchImpl, path(campaign, `/${encodeURIComponent(cueId)}`), { signal }, failure)
  const cue = await checkedCue(reply, campaign, failure)
  if (cue.cue_id !== cueId) throw new CueApiError(failure)
  return cue
}

export async function attachCue(cue: Cue, conversation: string, fetchImpl: typeof fetch): Promise<AttachedCueEntry> {
  const failure = "Couldn't add the cue to the GM thread. Try again."
  const data = CueAttachRequestSchema.safeParse({
    schema_version: CONTRACT_VERSION, conversation_id: conversation,
  })
  if (!data.success) throw new CueApiError(failure)
  const reply = await request(fetchImpl, path(cue.campaign_id, `/${encodeURIComponent(cue.cue_id)}/attach`), json('POST', data.data), failure)
  const entry = AttachedCueEntrySchema.safeParse(await body(reply))
  if (!reply.ok || !entry.success || entry.data.cue_id !== cue.cue_id) throw new CueApiError(failure)
  return entry.data
}

export interface CueUploadIntent {
  commandId: string
  assetId?: string
  saving?: boolean
}

async function checkedAsset(reply: Response, campaign: string, failure: string, expected?: string): Promise<Asset> {
  const payload = await body(reply)
  const asset = AssetSchema.safeParse(payload)
  if (asset.success && asset.data.campaign_id === campaign && asset.data.kind === 'audio' &&
      (expected === undefined || expected === asset.data.asset_id)) {
    return asset.data
  }
  const code = ErrorBodySchema.safeParse(payload).data?.detail.code
  if (code === 'cap_reached' || code === 'account_limit_reached') {
    throw new CueApiError('Media storage is full. Free up space and try again.')
  }
  throw new CueApiError(failure)
}

export async function discardCueUpload(campaign: string, intent: CueUploadIntent, fetchImpl: typeof fetch): Promise<void> {
  if (intent.saving || intent.assetId === undefined) return
  try {
    await request(fetchImpl, assetPath(campaign, intent.assetId), { method: 'DELETE' }, '')
  } catch {
    // Asset expiry still handles an abandoned upload.
  }
}

export async function uploadCue(
  campaign: string, title: string, kind: CueKind, file: File, intent: CueUploadIntent,
  signal: AbortSignal, fetchImpl: typeof fetch,
): Promise<Cue> {
  const extension = file.name.split('.').at(-1)?.toLowerCase() ?? ''
  const mediaType = AUDIO_TYPE[extension] ?? file.type
  const limit = kind === 'one_shot' ? ONE_SHOT_MAX_BYTES : AUDIO_MAX_BYTES
  if (file.size > limit) throw new CueApiError(`Choose a file under ${limit / 1_000_000} MB.`)
  const declared = AssetCreateRequestSchema.safeParse({
    schema_version: CONTRACT_VERSION, campaign_id: campaign, command_id: intent.commandId,
    kind: 'audio', media_type: mediaType, size_bytes: file.size, alt: null,
  })
  if (!declared.success) throw new CueApiError('Choose an MP3, M4A, Ogg or WAV audio file.')
  const cueRequest = {
    schema_version: CONTRACT_VERSION, campaign_id: campaign, command_id: intent.commandId, title, kind,
  }
  if (!CueCreateRequestSchema.safeParse({ ...cueRequest, asset_id: 'ast_preview' }).success) {
    throw new CueApiError('Enter a cue title.')
  }

  const failure = "Couldn't upload the sound. Try again."
  let asset: Asset
  if (intent.assetId === undefined) {
    const reserved = await request(fetchImpl, `/campaigns/${encodeURIComponent(campaign)}/assets`,
      json('POST', declared.data, signal), failure)
    asset = await checkedAsset(reserved, campaign, failure)
    intent.assetId = asset.asset_id
  } else {
    const status = await request(fetchImpl, `${assetPath(campaign, intent.assetId)}/status`, { signal }, failure)
    asset = await checkedAsset(status, campaign, failure, intent.assetId)
  }
  signal.throwIfAborted()
  if (asset.state === 'uploading') {
    const sent = await request(fetchImpl, `${assetPath(campaign, asset.asset_id)}/bytes`, {
      method: 'PUT', headers: { 'Content-Type': mediaType }, body: file, signal,
    }, failure)
    asset = await checkedAsset(sent, campaign, failure, intent.assetId)
  }
  signal.throwIfAborted()
  if (asset.state === 'failed') throw new CueApiError("This file couldn't be processed. Choose another sound.")
  if (asset.state !== 'ready') throw new CueApiError('The sound is still processing. Check again in a moment.')
  if (kind === 'one_shot' && (asset.duration_ms ?? 0) > ONE_SHOT_MAX_MS) {
    throw new CueApiError('A one-shot must be 30 seconds or shorter.')
  }
  intent.saving = true
  const saveFailure = "Couldn't save the cue. Try again."
  const saved = await request(fetchImpl, path(campaign), json('POST', {
    ...cueRequest, asset_id: asset.asset_id,
  }, signal), saveFailure)
  if (saved.status >= 400 && saved.status < 500) intent.saving = false
  return checkedCue(saved, campaign, saveFailure)
}

export async function previewPeaks(cue: Cue, signal: AbortSignal, fetchImpl: typeof fetch): Promise<number[] | undefined> {
  try {
    const reply = await request(fetchImpl, `${assetPath(cue.campaign_id, cue.asset_id)}?rendition=waveform`, { signal }, '')
    const data = await body(reply) as { schema_version?: unknown; peaks?: unknown } | null
    const peaks = data?.peaks
    return reply.ok && data?.schema_version === 1 && Array.isArray(peaks) && peaks.length <= 512 &&
      peaks.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1) ? peaks : undefined
  } catch {
    return undefined
  }
}
