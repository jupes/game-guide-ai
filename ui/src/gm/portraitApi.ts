import { notifyUnauthorized } from '../api'
import {
  AssetCreateRequestSchema, AssetRefSchema, AssetSchema, CONTRACT_VERSION, ErrorBodySchema,
  type Asset, type AssetRef,
} from './contracts'
import { assetPath } from './documentFields'

type PortraitUploadResult =
  | { kind: 'ready'; portrait: AssetRef }
  | { kind: 'failed'; message: string; startFresh?: boolean; assetId?: string }

function collection(campaignId: string): string {
  return `/campaigns/${encodeURIComponent(campaignId)}/assets`
}

async function request(fetchImpl: typeof fetch, path: string, init: RequestInit): Promise<Response | null> {
  try {
    const response = await fetchImpl(path, { ...init, credentials: 'include' })
    if (response.status === 401) notifyUnauthorized()
    return response
  } catch {
    return null
  }
}

async function readBody(response: Response | null): Promise<unknown> {
  try {
    return await response?.json()
  } catch {
    return undefined
  }
}

export async function canUploadPortrait(
  campaignId: string, signal: AbortSignal, fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  const response = await request(fetchImpl, `${collection(campaignId)}/upload-capability`, { signal })
  return response?.status === 204
}

export async function discardPortraitUpload(
  campaignId: string, assetId: string, fetchImpl: typeof fetch = fetch,
): Promise<void> {
  await request(fetchImpl, assetPath(campaignId, assetId), { method: 'DELETE' })
}

function portraitResult(asset: Asset): PortraitUploadResult {
  if (asset.state === 'failed') {
    const message = asset.failure === 'quota_exceeded'
      ? 'There is no room for that portrait. Try a smaller image.'
      : 'This image could not be processed. Try this file again or choose another one.'
    return { kind: 'failed', message, startFresh: true, assetId: asset.asset_id }
  }
  if (asset.state !== 'ready') {
    return { kind: 'failed', message: 'The image is still processing. Try again to check it.', assetId: asset.asset_id }
  }
  const reference = AssetRefSchema.safeParse({
    asset_id: asset.asset_id, media_type: 'image', alt: asset.alt, width: asset.width, height: asset.height,
  })
  return reference.success
    ? { kind: 'ready', portrait: reference.data }
    : { kind: 'failed', message: 'The portrait could not be read. Try again.', assetId: asset.asset_id }
}

export async function uploadPortrait(
  campaignId: string, file: File, alt: string, commandId: string, signal: AbortSignal, fetchImpl: typeof fetch = fetch,
): Promise<PortraitUploadResult> {
  const input = AssetCreateRequestSchema.safeParse({
    schema_version: CONTRACT_VERSION,
    campaign_id: campaignId,
    command_id: commandId,
    kind: 'image',
    media_type: file.type,
    size_bytes: file.size,
    alt,
  })
  if (!input.success) {
    return { kind: 'failed', message: 'Choose a PNG, JPEG, or WebP image and enter a short description.' }
  }
  const failed = { kind: 'failed', message: 'The portrait could not be uploaded. Try again.' } as const
  const response = await request(fetchImpl, collection(campaignId), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input.data), signal,
  })
  const avatarBody = await readBody(response)
  if (response?.status !== 201) {
    const error = ErrorBodySchema.safeParse(avatarBody)
    const code = error.data?.detail.code
    if (code === 'cap_reached' || code === 'account_limit_reached') {
      return { kind: 'failed', message: 'There is no room for another portrait. Free up media storage and try again.' }
    }
    return failed
  }

  const created = AssetSchema.safeParse(avatarBody)
  if (!created.success || created.data.campaign_id !== campaignId || created.data.kind !== 'image') return failed
  const asset = created.data
  const path = assetPath(campaignId, asset.asset_id)
  try {
    // A repeat of the same create intent can return the already processed asset.
    if (asset.state !== 'uploading') return portraitResult(asset)
    const sent = await request(fetchImpl, `${path}/bytes`, {
      method: 'PUT', headers: { 'Content-Type': file.type }, body: file, signal,
    })
    const processed = AssetSchema.safeParse(await readBody(sent))
    if (!processed.success || processed.data.campaign_id !== campaignId || processed.data.asset_id !== asset.asset_id ||
        processed.data.kind !== 'image') return { ...failed, assetId: asset.asset_id }
    return portraitResult(processed.data)
  } finally {
    // An explicitly abandoned intent never attached this newly created asset.
    if (signal.aborted) void discardPortraitUpload(campaignId, asset.asset_id, fetchImpl)
  }
}
