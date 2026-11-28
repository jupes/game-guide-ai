import type { Locator } from '@playwright/test'
import { expect, test } from './fixtures'
import { serveCueSound } from './cueSound'
import { installRevealRoutes, SESSION_ID, tableSessionAnswer } from './revealStubs'
import { CAMPAIGN_ID, DOCUMENT_ID, installWorkbenchRoutes, THREAD_PAGE } from './workbenchStubs'
import { AudioSettingsRequestSchema, CuePlayRequestSchema, CueStopRequestSchema, TableSessionAnswerSchema, TOOL_IDS, type Cue } from '../src/gm/contracts'

const prefix = `/campaigns/${CAMPAIGN_ID}`
const livePath = `${prefix}/sessions/${SESSION_ID}/audio`
const cue: Cue = {
  schema_version: 1, campaign_id: CAMPAIGN_ID, cue_id: 'cue_coastalRain0000000000000001',
  asset_id: 'ast_coastalRain0000000000000001', kind: 'ambience', title: 'Rain on the harbour',
  duration_ms: 16_020, archived: false, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:20:11Z',
}

test('a player enables sound once per page, mutes it, and releases playback on revocation', async ({ page }, info) => {
  const handle = 'm'.repeat(32)
  const started = new Date(Date.now() - 3500).toISOString()
  const postures: Array<{ state: string; playing_handles: string[] }> = []
  let waiting = false
  let active = true
  let mediaReads = 0
  const headers = { 'X-Table-Audio-Context': 'a'.repeat(64) }
  await page.route('**/table/stream?*', route => route.fulfill({ status: 404 }))
  await page.route('**/table/snapshot?*', route => route.fulfill({ headers, json: {
    schema_version: 1,
    frames: active ? [
      { schema_version: 1, event: 'session', role: 'participant', audio: true },
      { schema_version: 1, event: 'snapshot', slots: [
        { slot: 'table', seq: 1, content: { content_kind: 'document', type: 'npc', fields: [{ key: 'name', value: 'The harbour keeper' }] } },
        { slot: 'mine', seq: 0, content: null },
      ] },
      { schema_version: 1, event: 'audio', slot: 'ambience', seq: waiting ? 1 : 0, playing: waiting ? {
        asset: { handle, kind: 'audio', media_type: 'audio/mpeg', width: null, height: null, duration_ms: cue.duration_ms },
        started_at: started, start_offset_ms: 0, duration_ms: cue.duration_ms, loop: true,
      } : null },
      { schema_version: 1, event: 'ready' },
    ] : [{ schema_version: 1, event: 'inactive' }, { schema_version: 1, event: 'ready' }],
  } }))
  await page.route('**/table/presence?*', route => {
    const body = route.request().postDataJSON()
    if (body.action === 'open') return route.fulfill({ json: { schema_version: 1, registration: 'r'.repeat(32) } })
    if (body.action === 'report') postures.push(body)
    return route.fulfill({ status: 204 })
  })
  await page.route(`**/table/assets/${handle}`, route => { mediaReads++; return serveCueSound(route) })

  await page.goto(`/table?campaign_id=${CAMPAIGN_ID}`)
  const sound = page.getByRole('region', { name: 'Table sound' })
  const tap = sound.getByRole('button', { name: /Tap to enable sound/ })
  const mute = sound.getByRole('button', { name: 'Mute' })
  await expect(tap).toBeVisible()
  await expect(sound.getByText('Your GM is playing something')).toBeHidden()
  await expect.poll(() => postures.at(-1)?.state).toBe('pending')
  waiting = true
  await page.getByRole('button', { name: 'Reload table' }).click()
  await expect(sound.getByText('Your GM is playing something')).toBeVisible()
  expect(mediaReads).toBe(0)
  await page.screenshot({ path: info.outputPath('player-sound-prompt.png'), animations: 'disabled', scale: 'css' })
  await tap.click()
  await expect(mute).toBeFocused()
  await expect.poll(() => postures.at(-1)?.state).toBe('listening')
  expect(postures.at(-1)?.playing_handles).toEqual([handle])
  expect(mediaReads).toBeGreaterThan(0)
  await mute.click()
  await expect(mute).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => postures.at(-1)?.state).toBe('muted')
  await page.screenshot({ path: info.outputPath('player-sound-muted.png'), animations: 'disabled', scale: 'css' })
  await mute.click()
  await expect.poll(() => postures.at(-1)?.state).toBe('listening')
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByText('The harbour keeper')).toHaveCount(0)
  await expect.poll(() => postures.at(-1)?.state).toBe('listening')
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'visibilityState')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(page.getByText('The harbour keeper')).toBeVisible()
  await page.reload()
  await expect(tap).toBeVisible()
  await expect.poll(() => postures.at(-1)?.state).toBe('pending')
  await tap.click()
  await expect.poll(() => postures.at(-1)?.state).toBe('listening')
  active = false
  await page.getByRole('button', { name: 'Reload table' }).click()
  await expect(page.getByText('This table isn’t active or available to you.')).toBeVisible()
  await expect(sound).toBeHidden()
  await expect(page.getByText('The harbour keeper')).toHaveCount(0)
})

test('private preview stays local and the GM can stop live audio from another channel on a phone', async ({ page, isMobile }, info) => {
  const activate = (control: Locator) => isMobile ? control.tap() : control.click()
  await page.addInitScript(() => {
    const observed = new Set<HTMLMediaElement>()
    const play = HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play = function () { observed.add(this); return play.call(this) }
    Object.defineProperty(window, 'gmMonitorPlaying', { get: () => [...observed].some(media =>
      !media.isConnected && media.currentSrc.includes('/campaigns/') && !media.paused && media.currentTime > 0) })
  })
  await page.route('**/auth/me', (route) => route.fulfill({ json: { email: 'gm@example.com', role: 'dm' } }))
  await page.route('**/models', (route) => route.fulfill({ json: {
    default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }],
  } }))
  await page.route('**/metrics/ui', (route) => route.fulfill({ status: 204 }))
  await installWorkbenchRoutes(page)
  // A deployment with streaming disabled still serves the authenticated cue state and controls.
  await page.route(`**${prefix}/stream`, (route) => route.fulfill({ status: 404, json: {} }))
  const revealWrites: Array<{ path: string; body: unknown }> = []
  const reveals = await installRevealRoutes(page, { bodies: revealWrites })
  reveals.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name'] })
  await page.route((url) => url.pathname === '/conversations' && !url.searchParams.has('campaign_id'),
    (route) => route.fulfill({ json: THREAD_PAGE }))
  await page.route(`**${prefix}/assets/upload-capability`, (route) => route.fulfill({ status: 204 }))
  await page.route(`**${prefix}/tool-availability`, (route) => route.fulfill({
    json: Object.fromEntries(TOOL_IDS.map((id) => [id, null])),
  }))
  let enabled = true
  let epoch = 0
  await page.route(`**${prefix}/table-session`, (route) => {
    const { session } = TableSessionAnswerSchema.parse(tableSessionAnswer())
    return route.fulfill({ json: { schema_version: 1, session: { ...session, audio: enabled, audio_epoch: epoch } } })
  })
  await page.route(`**${prefix}/cues/capability`, (route) => route.fulfill({ status: 204 }))
  await page.route(`**${prefix}/cues/list`, (route) => route.fulfill({ json: {
    schema_version: 1, campaign_id: CAMPAIGN_ID, items: [cue], next_cursor: null,
  } }))
  await page.route(`**${prefix}/assets/${cue.asset_id}?rendition=waveform`, (route) => route.fulfill({ json: {
    schema_version: 1, peaks: [0.32, 0.55, 0.35, 0.27, 0.72, 0.65, 0.49, 0.77, 0.35, 0.58, 0.24, 0.63],
  } }))
  await page.route(`**${prefix}/assets/${cue.asset_id}`, serveCueSound)
  await page.route(`**${prefix}/cues/${cue.cue_id}`, route => route.fulfill({ json: cue }))
  await page.route(`**${livePath}/presence`, route => route.fulfill({
    headers: { 'X-Table-Audio-Enabled': enabled ? '1' : '0', 'X-Table-Audio-Epoch': String(epoch) }, json: {
    schema_version: 1, event: 'presence', session_id: SESSION_ID, gen: 1,
    participants: [{ participant_id: 'prt_ondrey', alias: 'Ondrey', audio: 'listening' }],
    guests: { connected: 1, muted: 0, listening: 0, pending: 1 },
  } }))

  let startedAt: string | null = null
  const commands: string[] = []
  const offAttempts: string[] = []
  await page.route(`**${livePath}/settings`, route => {
    const body = AudioSettingsRequestSchema.parse(route.request().postDataJSON())
    if (body.enabled) expect(body.audio_epoch).toBe(epoch)
    else {
      offAttempts.push(body.command_id)
      if (offAttempts.length === 1) return route.fulfill({ status: 503 })
      startedAt = null
    }
    enabled = body.enabled
    ++epoch
    return route.fulfill({ json: { schema_version: 1, enabled, audio_epoch: epoch } })
  })
  const picture = () => ['ambience', 'one_shot'].map((slot) => ({
    schema_version: 1, event: 'audio', session_id: SESSION_ID, gen: 1, audio_epoch: epoch,
    slot, seq: slot === 'ambience' ? epoch : 0,
    playing: slot === 'ambience' && startedAt ? {
      cue_id: cue.cue_id, title: cue.title, started_at: startedAt, start_offset_ms: 0, loop: true, duration_ms: cue.duration_ms,
    } : null,
  }))
  await page.route(`**${livePath}`, (route) => route.fulfill({ json: picture() }))
  await page.route(`**${livePath}/play`, (route) => {
    const body = CuePlayRequestSchema.parse(route.request().postDataJSON())
    expect(body).toMatchObject({ audio_epoch: epoch, cue_id: cue.cue_id, loop: true })
    startedAt = new Date().toISOString()
    ++epoch
    commands.push('play')
    return route.fulfill({ json: picture() })
  })
  await page.route(`**${livePath}/stop`, (route) => {
    const body = CueStopRequestSchema.parse(route.request().postDataJSON())
    expect([null, cue.cue_id]).toContain(body.cue_id)
    startedAt = null
    ++epoch
    commands.push(body.cue_id === null ? 'stop all' : 'stop cue')
    return route.fulfill({ json: picture() })
  })

  if (!isMobile) await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}&document=${DOCUMENT_ID}`)
  const library = page.getByRole('region', { name: 'Campaign Library' })
  if (isMobile) {
    await activate(page.getByRole('button', { name: 'Open navigation' }))
    const drawer = page.getByRole('dialog', { name: 'Navigation' })
    await activate(drawer.getByRole('button', { name: 'Cues', exact: true }))
    await expect(drawer).toBeHidden()
  } else {
    await activate(page.getByRole('navigation', { name: 'Navigation rail' }).getByRole('button', { name: 'Campaign Library' }))
    await activate(library.getByRole('tab', { name: 'Cues' }))
  }
  await expect(library.getByRole('tab', { name: 'Cues' })).toHaveAttribute('aria-selected', 'true')
  await activate(library.getByRole('button', { name: cue.title, exact: true }))
  const card = library.getByRole('region', { name: cue.title })
  const live = page.getByRole('region', { name: 'Live table audio' })
  const pill = page.getByRole('group', { name: 'Live at the table' })
  const playing = isMobile ? pill.getByRole('button', { name: '2 live' }) : live.getByText(cue.title)
  const liveStatus = isMobile ? playing : live
  await activate(card.getByRole('button', { name: `Play preview of ${cue.title}` }))
  await expect(card.getByRole('button', { name: `Pause preview of ${cue.title}` })).toBeVisible()
  await expect.poll(() => library.locator('audio').evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0)
  await activate(card.getByRole('button', { name: `Pause preview of ${cue.title}` }))
  await expect(liveStatus).toBeHidden()
  expect(commands).toEqual([])
  const play = card.getByRole('button', { name: 'Play to table' })
  await activate(play)
  await expect(playing).toBeVisible()
  await expect.poll(() => page.evaluate(() => Reflect.get(window, 'gmMonitorPlaying'))).toBe(true)
  const status = isMobile ? page.getByRole('group', { name: 'Table session' }) : live
  await expect(status.getByText(/1 listening · 0 muted · 1 screen/)).toBeVisible()
  expect(commands).toEqual(['play'])
  const sound = page.getByRole('switch', { name: 'Table audio' })
  await expect(sound).toBeChecked()
  await activate(sound)
  await expect.poll(() => offAttempts.length).toBe(2)
  expect(offAttempts[0]).toBe(offAttempts[1])
  await expect(liveStatus).toBeHidden()
  await expect(play).toBeDisabled()
  await expect.poll(() => page.evaluate(() => Reflect.get(window, 'gmMonitorPlaying'))).toBe(false)
  await activate(sound)
  await expect(play).toBeEnabled()
  await activate(play)
  const stopCue = card.getByRole('button', { name: `Stop ${cue.title}` })
  await activate(isMobile ? stopCue : live.getByRole('button', { name: 'Stop all' }))
  await expect(liveStatus).toBeHidden()
  const audioOnlyStop = isMobile ? 'stop cue' : 'stop all'
  expect(commands).toEqual(['play', 'play', audioOnlyStop])
  expect(revealWrites).toEqual([])
  await activate(play)
  await expect(playing).toBeVisible()
  if (!isMobile) {
    await stopCue.scrollIntoViewIfNeeded()
    await page.screenshot({ path: info.outputPath('desktop-live.png'), animations: 'disabled', scale: 'css' })
    await page.setViewportSize({ width: 390, height: 844 })
  }

  const stop = pill.getByRole('button', { name: 'Stop all', exact: true })
  await expect(stop).toBeInViewport()
  await stopCue.scrollIntoViewIfNeeded()
  await page.screenshot({ path: info.outputPath('phone-cues.png'), animations: 'disabled', scale: 'css' })
  await activate(library.getByRole('button', { name: 'Back', exact: true }))
  await activate(page.getByRole('navigation', { name: 'Channels' }).getByRole('button', { name: 'Sage' }))
  await expect(library).toBeHidden()
  await activate(pill.getByRole('button', { name: '2 live' }))
  const soundsAndReveals = pill.getByRole('list', { name: 'Live reveals and sounds' })
  await expect(soundsAndReveals.getByRole('button', { name: `Stop ${cue.title}` })).toBeVisible()
  await expect(soundsAndReveals.getByText(/Ondrey/).first()).toBeVisible()
  await expect(stop).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('phone-sage-live.png'), animations: 'disabled', scale: 'css' })
  await activate(page.getByRole('button', { name: 'Open navigation' }))
  const drawer = page.getByRole('dialog', { name: 'Navigation' })
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: 'Close navigation' }).focus()
  await page.keyboard.press('Tab')
  await expect(drawer.getByRole('button', { name: 'Stop all table sharing' })).toBeFocused()
  await activate(stop)
  await expect(pill).toBeHidden()
  expect(commands).toEqual(['play', 'play', audioOnlyStop, 'play', 'stop all'])
  expect(revealWrites).toEqual([{
    path: `${prefix}/reveals/stop`, body: { schema_version: 1, command_id: expect.any(String), scope: 'all' },
  }])
})
