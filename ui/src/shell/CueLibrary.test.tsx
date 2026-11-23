import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Asset, Cue, CueListQuery } from '../gm/contracts'
import { CueLibrary } from './CueLibrary'

const campaign = 'cmp_TestCampaign00000000001'
const assetId = 'ast_TestAudio00000000000001'
const cueId = 'cue_TestCue0000000000000001'
const prefix = `/campaigns/${campaign}`
const assetPath = `${prefix}/assets/${assetId}`
const now = '2026-01-01T00:00:00Z'
const cue: Cue = {
  schema_version: 1, campaign_id: campaign, cue_id: cueId, asset_id: assetId, title: 'Rain',
  kind: 'ambience', archived: false, duration_ms: 12000, created_at: now, updated_at: now,
}
const uploading: Asset = {
  schema_version: 1, asset_id: assetId, campaign_id: campaign, kind: 'audio', state: 'uploading',
  media_type: 'audio/wav', size_bytes: 6, width: null, height: null, duration_ms: null, alt: null,
  failure: null, created_at: now, updated_at: now,
}
const ready: Asset = { ...uploading, state: 'ready', media_type: 'audio/mpeg', duration_ms: 12000 }

function page(items: Cue[], next_cursor: string | null = null): Response {
  return Response.json({ schema_version: 1, campaign_id: campaign, items, next_cursor })
}

function fakeFetch(handler: (path: string, init?: RequestInit) => Response) {
  return vi.fn<typeof fetch>(async (input, init) => handler(String(input), init))
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Cue library', () => {
  it('retries processing without resending audio, then opens a private preview', async () => {
    let stored: Cue | undefined
    const fetchImpl = fakeFetch((path, init) => {
      if (path === `${prefix}/cues/list`) return page(stored ? [stored] : [])
      if (path === `${prefix}/assets`) {
        expect(init?.body).not.toContain('private-source-name')
        return Response.json(uploading, { status: 201 })
      }
      if (path === `${assetPath}/bytes`) {
        expect(init?.method).toBe('PUT')
        return Response.json({ ...uploading, state: 'processing' }, { status: 202 })
      }
      if (path === `${assetPath}/status`) return Response.json(ready)
      if (path === `${prefix}/cues`) {
        const { kind, title } = JSON.parse(String(init?.body)) as Pick<Cue, 'kind' | 'title'>
        stored = { ...cue, kind, title }
        return Response.json(stored, { status: 201 })
      }
      if (path === `${assetPath}?rendition=waveform`) {
        return Response.json({ schema_version: 1, peaks: [0, 0.5, 1] })
      }
      throw new Error(`Unexpected fetch for ${path}`)
    })
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play')
      .mockRejectedValue(new DOMException('Browser blocked playback', 'NotAllowedError'))
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
    const announce = vi.fn()
    const user = userEvent.setup()
    const { container } = render(<CueLibrary campaign={campaign} fetchImpl={fetchImpl} announce={announce} />)

    await screen.findByText('Upload a sound to get started.')
    await user.click(screen.getByRole('button', { name: 'Upload sound' }))
    const title = screen.getByRole('textbox', { name: 'Cue title' })
    expect(title).toHaveFocus()
    await user.type(title, 'Chime')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Kind' }), 'one_shot')
    await user.upload(screen.getByLabelText('Sound file'), new File(['sample'], 'private-source-name.wav', { type: 'audio/wav' }))
    await user.click(screen.getByRole('button', { name: 'Upload' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('still processing')
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    const preview = await screen.findByRole('button', { name: 'Play preview of Chime' })
    expect(announce).toHaveBeenCalledWith('Uploaded Chime')
    expect(screen.queryByRole('button', { name: /loop chime|play to table/i })).toBeNull()
    expect(container.querySelector('audio')).not.toHaveAttribute('src')
    await user.click(preview)
    expect(play).toHaveBeenCalledOnce()
    expect(container.querySelector('audio')).toHaveAttribute('src', assetPath)
    expect(await screen.findByText('Press play again to allow sound')).toBeVisible()
    expect(fetchImpl.mock.calls.filter(([url]) => url === `${assetPath}/bytes`)).toHaveLength(1)
    expect(fetchImpl.mock.calls.filter(([, init]) => init?.method === 'DELETE')).toHaveLength(0)
  })

  it('paginates, searches, renames, and recovers an archived cue through either control', async () => {
    let rain = cue
    let failedPage = false
    const bell = { ...cue, cue_id: 'cue_TestBell000000000000001', title: 'Bell' }
    const fetchImpl = fakeFetch((path, init) => {
      if (path === `${prefix}/cues/list`) {
        const query = JSON.parse(String(init?.body)) as CueListQuery
        if (query.search) return page([])
        if (query.archived) return page(rain.archived ? [rain] : [])
        if (query.cursor) {
          if (!failedPage) {
            failedPage = true
            return new Response(null, { status: 503 })
          }
          return page([bell])
        }
        return rain.archived ? page([bell]) : page([rain], cueId)
      }
      if (path === `${prefix}/cues/${cueId}` && init?.method === 'PATCH') {
        const { title } = JSON.parse(String(init.body)) as Pick<Cue, 'title'>
        rain = { ...rain, title }
        return Response.json(rain)
      }
      if (path === `${prefix}/cues/${cueId}/archive` || path === `${prefix}/cues/${cueId}/restore`) {
        rain = { ...rain, archived: path.endsWith('/archive') }
        return Response.json(rain)
      }
      return new Response(null, { status: 404 })
    })
    const user = userEvent.setup()
    render(<CueLibrary campaign={campaign} fetchImpl={fetchImpl} announce={vi.fn()} />)

    await user.click(await screen.findByRole('button', { name: 'Load more' }))
    await user.click(await screen.findByRole('button', { name: 'Retry load more' }))
    expect(await screen.findByRole('button', { name: 'Bell' })).toBeVisible()
    const search = screen.getByRole('textbox', { name: 'Search cues' })
    await user.type(search, 'missing')
    await user.click(await screen.findByRole('button', { name: 'Clear search' }))
    expect(search).toHaveFocus()
    await user.click(await screen.findByRole('button', { name: 'Rain' }))
    await user.click(screen.getByRole('button', { name: 'Rename' }))
    const title = screen.getByRole('textbox', { name: 'Cue title' })
    expect(title).toHaveFocus()
    await user.clear(title)
    await user.type(title, 'Storm')
    await user.click(screen.getByRole('button', { name: 'Save title' }))

    await screen.findByRole('button', { name: 'Storm' })
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    await user.click(await screen.findByRole('button', { name: 'Storm' }))
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Storm' })).toBeNull())
    const filter = screen.getByRole('combobox', { name: 'Show' })
    await user.selectOptions(filter, 'archived')
    await user.click(await screen.findByRole('button', { name: 'Storm' }))
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    expect(await screen.findByText('No archived cues.')).toBeVisible()
  })
})
