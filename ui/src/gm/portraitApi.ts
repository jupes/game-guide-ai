import { notifyUnauthorized } from '../api'
import { tableReadRetryAt as retryAfterDeadline } from '../table/readResponse'
import {
  AssetCreateRequestSchema, AssetRefSchema, AssetSchema, AttachedImageEntrySchema, CONTRACT_VERSION, ErrorBodySchema,
  ImageAttachRequestSchema, type Asset, type AssetRef, type AttachedImageEntry, type ImageUse,
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
    init.signal?.throwIfAborted()
    const response = await fetchImpl(path, { ...init, credentials: 'include', mode: 'same-origin', redirect: 'error' })
    if (response.status === 401 && !init.signal?.aborted) notifyUnauthorized()
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
  campaignId: string, assetId: string, fetchImpl: typeof fetch = fetch, signal?: AbortSignal,
): Promise<{ kind: 'gone' } | { kind: 'retry'; after: number }> {
  const response = await request(fetchImpl, assetPath(campaignId, assetId), { method: 'DELETE', signal })
  if (response?.status === 204) return { kind: 'gone' }
  const detail = response?.headers.get('content-type')?.split(';')[0].trim().toLowerCase() === 'application/json'
    ? ErrorBodySchema.safeParse(await readBody(response)).data?.detail : null
  // A prior DELETE may have succeeded even when its acknowledgement was lost.
  if (response?.status === 404 && detail?.code === 'not_found') return { kind: 'gone' }
  return { kind: 'retry', after: response?.status === 429 ? retryAfterDeadline(response) : 0 }
}

type AttachImageResult =
  | { kind: 'attached'; entry: AttachedImageEntry }
  | { kind: 'refused' | 'unconfirmed'; message: string }

export async function attachThreadImage(
  campaignId: string, thread: string, assetId: string, use: ImageUse, signal: AbortSignal, fetchImpl: typeof fetch = fetch,
): Promise<AttachImageResult> {
  const body = ImageAttachRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, conversation_id: thread, use })
  if (!body.success) return { kind: 'refused', message: 'Couldn’t open this GM thread. Reopen it and try again.' }
  const response = await request(fetchImpl, `${assetPath(campaignId, assetId)}/attach`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body.data), signal,
  })
  const reply = await readBody(response)
  if (response && response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() === 'application/json') {
    const code = ErrorBodySchema.safeParse(reply).data?.detail.code
    if (response.status === 404 && code === 'not_found') {
      return { kind: 'refused', message: 'This image or GM thread is unavailable. Choose another image or reopen the thread.' }
    }
    if (response.status === 429 && code === 'throttled_user') {
      return { kind: 'refused', message: 'Wait a moment, then try adding the same image again.' }
    }
    if ((response.status === 422 && code === 'invalid') || (response.status === 403 && code === 'forbidden')) {
      return { kind: 'refused', message: 'Couldn’t add this image. Reopen the thread and try again.' }
    }
  }
  const entry = response?.status === 200 ? AttachedImageEntrySchema.safeParse(reply) : null
  if (!entry?.success || entry.data.asset_id !== assetId || entry.data.use !== use) {
    return { kind: 'unconfirmed', message: 'Couldn’t confirm the attachment. Try again with the same image.' }
  }
  return { kind: 'attached', entry: entry.data }
}

export async function readThreadImage(
  campaignId: string, assetId: string, signal: AbortSignal, fetchImpl: typeof fetch = fetch,
): Promise<AssetRef | null> {
  const response = await request(fetchImpl, `${assetPath(campaignId, assetId)}/status`, { signal, cache: 'no-store' })
  const parsed = response?.ok ? AssetSchema.safeParse(await readBody(response)) : null
  if (!parsed?.success || signal.aborted) return null
  const asset = parsed.data
  if (asset.campaign_id !== campaignId || asset.asset_id !== assetId || asset.kind !== 'image') return null
  const result = portraitResult(asset)
  return result.kind === 'ready' ? result.portrait : null
}

function portraitResult(asset: Asset): PortraitUploadResult {
  if (asset.state === 'failed') {
    const message = asset.failure === 'quota_exceeded'
      ? 'There is no room for that image. Try a smaller file.'
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
    : { kind: 'failed', message: 'The image could not be read. Try again.', assetId: asset.asset_id }
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
  const failed = { kind: 'failed', message: 'The image could not be uploaded. Try again.' } as const
  const response = await request(fetchImpl, collection(campaignId), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input.data), signal,
  })
  const avatarBody = await readBody(response)
  if (response?.status !== 201) {
    const error = ErrorBodySchema.safeParse(avatarBody)
    const code = error.data?.detail.code
    if (code === 'cap_reached' || code === 'account_limit_reached') {
      return { kind: 'failed', message: 'There is no room for another image. Free up media storage and try again.' }
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
