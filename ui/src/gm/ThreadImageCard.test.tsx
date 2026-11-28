import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ThreadImageCard } from './ThreadImageCard'
import { documentFixture } from './documentFixtures'

const npc = documentFixture('npc')
const handout = documentFixture('handout')
const campaignId = npc.campaign_id
const asset = { asset_id: 'ast_map', media_type: 'image' as const, alt: 'Archive floor plan', width: 500, height: 350 }
const path = `/campaigns/${campaignId}/assets/${asset.asset_id}`
const time = '2026-09-14T11:02:00Z'
const metadata = { schema_version: 1, ...asset, campaign_id: campaignId, media_type: 'image/png',
  size_bytes: 4020, kind: 'image', state: 'ready', duration_ms: null, failure: null, created_at: time, updated_at: time }
const source = { campaignId, assetId: asset.asset_id, use: 'map' as const, entryId: 'ent_image' }
const row = { document_id: npc.document_id, type: npc.type, title: npc.data.name, qualifier: '',
  tags: [], archived: false, updated_at: time }
const page = { schema_version: 1, campaign_id: campaignId, category: 'npcs', items: [row], next_cursor: null }
const response = (body: unknown, status = 200) => Response.json(body, { status })
const scope = () => ({ actorId: 'gm', conversationId: 'cnv_thread', onSaved: vi.fn(), acceptDocument: vi.fn() })
function pending<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(complete => { resolve = complete })
  return { promise, resolve }
}

describe('GM thread image card', () => {
  it('reads only the scoped ready status; the viewer traps focus, closes with Escape and returns to the card', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response(metadata))
    const { unmount } = render(<ThreadImageCard {...source} scope={scope()} fetchImpl={request} />)
    const expand = await screen.findByRole('button', { name: 'Expand image' })
    expect(request.mock.calls.map(([url]) => url)).toEqual([path + '/status'])
    expect(screen.getByText('Map')).toBeVisible()
    expect(expand).toHaveAccessibleDescription(`Map ${asset.alt}`)
    expect(within(expand).getByAltText(asset.alt)).toHaveAttribute('src', path + '?rendition=thumb')
    fireEvent.click(expand)
    const viewer = screen.getByRole('dialog', { name: 'Map' })
    expect(viewer).toHaveFocus()
    expect(within(viewer).getByAltText(asset.alt)).toHaveAttribute('src', path)
    fireEvent.keyDown(viewer, { key: 'Tab', shiftKey: true })
    expect(within(viewer).getByRole('button', { name: 'Link to document' })).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(within(viewer).getByRole('button', { name: 'Close' })).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(expand).toHaveFocus()
    expect(request).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('drops late metadata from another actor and rejects a ready asset from another campaign', async () => {
    const old = pending<Response>()
    const request = vi.fn<typeof fetch>().mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(response({ ...metadata, campaign_id: 'cmp_other' }))
    const view = render(<ThreadImageCard {...source} scope={scope()} fetchImpl={request} />)
    view.rerender(<ThreadImageCard {...source} scope={{ ...scope(), actorId: 'new-gm' }} fetchImpl={request} />)
    await screen.findByText('This image is unavailable.')
    await act(async () => { old.resolve(response(metadata)) })
    expect(screen.queryByRole('button', { name: 'Expand image' })).toBeNull()
    expect(request.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })

  it('withdraws the open viewer and moves focus to recovery when the full image has been removed', () => {
    render(<ThreadImageCard {...source} knownAsset={asset} scope={scope()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Expand image' }))
    fireEvent.error(within(screen.getByRole('dialog')).getByAltText(asset.alt))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByAltText(asset.alt)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save as handout' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Retry image' })).toHaveFocus()
  })

  it('retries the immutable entry after a lost promotion reply and opens a saved handout only when asked', async () => {
    const held = pending<Response>()
    const request = vi.fn<typeof fetch>().mockReturnValueOnce(held.promise).mockResolvedValueOnce(response(handout))
    const actions = scope(), onOpen = vi.fn()
    render(<ThreadImageCard {...source} knownAsset={asset} scope={actions} onOpen={onOpen} fetchImpl={request} />)
    const save = screen.getByRole('button', { name: 'Save as handout' })
    fireEvent.click(save); fireEvent.click(save)
    expect(request).toHaveBeenCalledTimes(1)
    await act(async () => { held.resolve(response({}, 503)) })
    fireEvent.click(screen.getByRole('button', { name: 'Save as handout' }))
    const open = await screen.findByRole('button', { name: 'Open handout' })
    expect(request.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual(
      Array(2).fill([`/campaigns/${campaignId}/conversations/cnv_thread/timeline/ent_image/promote`, 'POST', '{}']),
    )
    expect(actions.onSaved).toHaveBeenCalledTimes(1)
    expect(onOpen).not.toHaveBeenCalled()
    fireEvent.click(open)
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ document_id: handout.document_id, type: 'handout' }))
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('does not deliver a pending save to a new campaign or thread', async () => {
    const held = pending<Response>()
    const request = vi.fn<typeof fetch>().mockReturnValueOnce(held.promise)
    const actions = scope()
    const view = render(<ThreadImageCard {...source} knownAsset={asset} scope={actions} fetchImpl={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save as handout' }))
    view.rerender(<ThreadImageCard {...source} knownAsset={asset} scope={{ ...actions, conversationId: 'cnv_other' }} fetchImpl={request} />)
    await act(async () => { held.resolve(response(handout)) })
    expect(actions.onSaved).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Save as handout' })).toBeEnabled()
  })

  it('offers to open the original archived handout without restoring or recreating it', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...handout, archived: true }))
    const onOpen = vi.fn()
    render(<ThreadImageCard {...source} knownAsset={asset} scope={scope()} onOpen={onOpen} fetchImpl={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Save as handout' }))
    const open = await screen.findByRole('button', { name: 'Open archived handout' })
    expect(screen.getByRole('status')).toHaveTextContent('The saved handout is archived.')
    expect(onOpen).not.toHaveBeenCalled()
    fireEvent.click(open)
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ document_id: handout.document_id }))
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('requires a fresh review and explicit confirmation after a conflicting replacement', async () => {
    const actions = scope(), written = { ...npc, write_revision: 10, data: { ...npc.data, portrait: asset } }
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response(metadata))
      .mockResolvedValueOnce(response(page)).mockResolvedValueOnce(response(npc))
      .mockResolvedValueOnce(response({ detail: { code: 'conflict', message: 'Updated', retryable: true,
        conflict: { write_revision: 9, fields: ['portrait'] } } }, 409))
      .mockResolvedValueOnce(response({ ...npc, write_revision: 9 })).mockResolvedValueOnce(response(written))
    render(<ThreadImageCard {...source} scope={actions} fetchImpl={request} />)
    const opener = await screen.findByRole('button', { name: 'Link to document' })
    fireEvent.click(opener)
    const viewer = within(screen.getByRole('dialog'))
    const types = viewer.getByRole('combobox', { name: 'Document type' })
    expect(types).toHaveFocus()
    expect(Array.from((types as HTMLSelectElement).options, item => item.value)).toEqual(['npc', 'handout', 'character-sheet'])
    fireEvent.change(await viewer.findByLabelText('Document', { exact: true }), { target: { value: npc.document_id } })
    const replace = await viewer.findByRole('button', { name: 'Replace image' })
    expect(request).toHaveBeenCalledTimes(3)
    fireEvent.click(replace)
    fireEvent.click(await viewer.findByRole('button', { name: 'Review current document' }))
    const reviewed = await viewer.findByRole('button', { name: 'Replace image' })
    expect(request).toHaveBeenCalledTimes(5)
    fireEvent.click(reviewed)
    await viewer.findByText('Image linked to document.')
    expect(viewer.getByRole('button', { name: 'Link to document' })).toHaveFocus()
    expect(actions.onSaved).toHaveBeenCalledOnce()
    expect(actions.acceptDocument).toHaveBeenCalledWith(written)
    expect(request.mock.calls.filter(([, init]) => init?.method === 'PATCH').map(([, init]) => JSON.parse(String(init?.body))))
      .toEqual([npc.write_revision, 9].map(revision => ({ schema_version: 1, type: 'npc', type_version: 1,
        base_write_revision: revision, fields: { portrait: asset } })))
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(opener).toHaveFocus()
  })

  it('uses current authorized metadata to link a historical media result, preserving read-only expansion', async () => {
    const stale = { ...asset, alt: 'Historical description', width: 40, height: 30 }
    const written = { ...npc, write_revision: 8, data: { ...npc.data, portrait: asset } }
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response(page)).mockResolvedValueOnce(response(npc))
      .mockResolvedValueOnce(response(metadata)).mockResolvedValueOnce(response(written))
    render(<ThreadImageCard {...source} knownAsset={stale} scope={scope()} fetchImpl={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Expand image' }))
    const modal = within(screen.getByRole('dialog'))
    expect(modal.getByAltText(stale.alt)).toHaveAttribute('src', path)
    expect(request).not.toHaveBeenCalled()
    fireEvent.click(modal.getByRole('button', { name: 'Link to document' }))
    fireEvent.change(await modal.findByLabelText('Document', { exact: true }), { target: { value: npc.document_id } })
    fireEvent.click(await modal.findByRole('button', { name: 'Replace image' }))
    await modal.findByText('Image linked to document.')
    expect(request.mock.calls[2][0]).toBe(path + '/status')
    expect(JSON.parse(String(request.mock.calls[3][1]?.body)).fields.portrait).toEqual(asset)
  })

  it('drops a deferred asset check on a campaign change before sending a portrait write', async () => {
    const check = pending<Response>(), actions = scope()
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response(page)).mockResolvedValueOnce(response(npc))
      .mockReturnValueOnce(check.promise)
    const view = render(<ThreadImageCard {...source} knownAsset={asset} scope={actions} fetchImpl={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Link to document' }))
    const modal = within(screen.getByRole('dialog'))
    fireEvent.change(await modal.findByLabelText('Document', { exact: true }), { target: { value: npc.document_id } })
    fireEvent.click(await modal.findByRole('button', { name: 'Replace image' }))
    expect(request.mock.calls.at(-1)?.[0]).toBe(path + '/status')
    view.rerender(<ThreadImageCard {...source} campaignId="cmp_other" knownAsset={asset} scope={actions} fetchImpl={request} />)
    await act(async () => { check.resolve(response(metadata)) })
    expect(request).toHaveBeenCalledTimes(3)
    expect(actions.acceptDocument).not.toHaveBeenCalled()
  })

  it('delivers a verified PATCH to the scoped canvas after the picker closes', async () => {
    const write = pending<Response>(), actions = scope()
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response(metadata)).mockResolvedValueOnce(response(page))
      .mockResolvedValueOnce(response(npc)).mockReturnValueOnce(write.promise)
    render(<ThreadImageCard {...source} scope={actions} fetchImpl={request} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Link to document' }))
    const dialog = screen.getByRole('dialog'), modal = within(dialog)
    fireEvent.change(await modal.findByLabelText('Document', { exact: true }), { target: { value: npc.document_id } })
    fireEvent.click(await modal.findByRole('button', { name: 'Replace image' }))
    fireEvent.keyDown(dialog, { key: 'Escape' })
    const written = { ...npc, write_revision: 9, data: { ...npc.data, portrait: asset } }
    await act(async () => { write.resolve(response(written)) })
    expect(actions.acceptDocument).toHaveBeenCalledWith(written)
    expect(actions.onSaved).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('ignores archived or wrong-campaign library rows and never writes to an unverified selection', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(response({ ...page, campaign_id: 'cmp_other' }))
      .mockResolvedValueOnce(response({ ...page, items: [{ ...row, archived: true }] }))
    render(<ThreadImageCard {...source} knownAsset={asset} scope={scope()} fetchImpl={request} />)
    fireEvent.click(screen.getByRole('button', { name: 'Link to document' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry documents' }))
    await waitFor(() => expect(screen.getByText('No matching documents.')).toBeVisible())
    expect(screen.queryByRole('option', { name: String(npc.data.name) })).toBeNull()
    expect(request).toHaveBeenCalledTimes(2)
  })
})
