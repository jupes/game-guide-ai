import { expect, test } from './fixtures'
import { installRevealRoutes, SESSION_ID, tableSessionAnswer } from './revealStubs'
import { CAMPAIGN_ID, DOCUMENT_ID, installWorkbenchRoutes, THREAD_PAGE } from './workbenchStubs'
import { CuePlayRequestSchema, CueStopRequestSchema, TableSessionAnswerSchema, TOOL_IDS, type Cue } from '../src/gm/contracts'

const prefix = `/campaigns/${CAMPAIGN_ID}`
const livePath = `${prefix}/sessions/${SESSION_ID}/audio`
const cue: Cue = {
  schema_version: 1, campaign_id: CAMPAIGN_ID, cue_id: 'cue_coastalRain0000000000000001',
  asset_id: 'ast_coastalRain0000000000000001', kind: 'ambience', title: 'Rain on the harbour',
  duration_ms: 16_020, archived: false, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:20:11Z',
}

// MPEG-2 Layer III, 8 kb/s mono at 16 kHz. A 36-byte frame with zero side
// information and zero spectral data encodes 576 silent samples (36 ms).
const silentFrame = Buffer.concat([Buffer.from([0xff, 0xf3, 0x18, 0xc4]), Buffer.alloc(32)])
const sound = Buffer.concat(Array.from({ length: 445 }, () => silentFrame))

test('private preview stays local and the GM can stop live audio from another channel on a phone', async ({ page }, info) => {
  await page.route('**/auth/me', (route) => route.fulfill({ json: { email: 'gm@example.com', role: 'dm' } }))
  await page.route('**/models', (route) => route.fulfill({ json: {
    default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }],
  } }))
  await page.route('**/metrics/ui', (route) => route.fulfill({ status: 204 }))
  await installWorkbenchRoutes(page)
  const revealWrites: Array<{ path: string; body: unknown }> = []
  const reveals = await installRevealRoutes(page, { bodies: revealWrites })
  reveals.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name'] })
  await page.route((url) => url.pathname === '/conversations' && !url.searchParams.has('campaign_id'),
    (route) => route.fulfill({ json: THREAD_PAGE }))
  await page.route(`**${prefix}/assets/upload-capability`, (route) => route.fulfill({ status: 204 }))
  await page.route(`**${prefix}/tool-availability`, (route) => route.fulfill({
    json: Object.fromEntries(TOOL_IDS.map((id) => [id, null])),
  }))
  await page.route(`**${prefix}/table-session`, (route) => {
    const { session } = TableSessionAnswerSchema.parse(tableSessionAnswer())
    return route.fulfill({ json: { schema_version: 1, session: { ...session, audio: true } } })
  })
  await page.route(`**${prefix}/cues/capability`, (route) => route.fulfill({ status: 204 }))
  await page.route(`**${prefix}/cues/list`, (route) => route.fulfill({ json: {
    schema_version: 1, campaign_id: CAMPAIGN_ID, items: [cue], next_cursor: null,
  } }))
  await page.route(`**${prefix}/assets/${cue.asset_id}?rendition=waveform`, (route) => route.fulfill({ json: {
    schema_version: 1, peaks: [0.32, 0.55, 0.35, 0.27, 0.72, 0.65, 0.49, 0.77, 0.35, 0.58, 0.24, 0.63],
  } }))
  await page.route(`**${prefix}/assets/${cue.asset_id}`, (route) => {
    const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range ?? '')
    const headers = { 'Accept-Ranges': 'bytes' }
    if (range === null) return route.fulfill({ headers, contentType: 'audio/mpeg', body: sound })
    const start = Number(range[1])
    const end = Math.min(range[2] ? Number(range[2]) : sound.length - 1, sound.length - 1)
    return route.fulfill({ status: 206, contentType: 'audio/mpeg', body: sound.subarray(start, end + 1),
      headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${sound.length}` },
    })
  })

  let epoch = 0
  let startedAt: string | null = null
  const commands: string[] = []
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
    expect(body.cue_id).toBeNull()
    startedAt = null
    ++epoch
    commands.push('stop all')
    return route.fulfill({ json: picture() })
  })

  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}&document=${DOCUMENT_ID}`)
  await page.getByRole('navigation', { name: 'Navigation rail' }).getByRole('button', { name: 'Campaign Library' }).click()
  const library = page.getByRole('region', { name: 'Campaign Library' })
  await library.getByRole('tab', { name: 'Cues' }).click()
  await library.getByRole('button', { name: cue.title, exact: true }).click()
  const card = library.getByRole('region', { name: cue.title })
  const live = page.getByRole('region', { name: 'Live table audio' })
  await card.getByRole('button', { name: `Play preview of ${cue.title}` }).click()
  await expect(card.getByRole('button', { name: `Pause preview of ${cue.title}` })).toBeVisible()
  await expect.poll(() => library.locator('audio').evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0)
  await card.getByRole('button', { name: `Pause preview of ${cue.title}` }).click()
  await expect(live).toBeHidden()
  expect(commands).toEqual([])
  await card.getByRole('button', { name: 'Play to table' }).click()
  await expect(live.getByText(cue.title)).toBeVisible()
  expect(commands).toEqual(['play'])
  await live.getByRole('button', { name: 'Stop all' }).click()
  await expect(live).toBeHidden()
  expect(commands).toEqual(['play', 'stop all'])
  expect(revealWrites).toEqual([])
  await card.getByRole('button', { name: 'Play to table' }).click()
  await expect(live.getByText(cue.title)).toBeVisible()
  await card.getByRole('button', { name: `Stop ${cue.title}` }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: info.outputPath('desktop-live.png'), animations: 'disabled' })

  await page.setViewportSize({ width: 390, height: 844 })
  const pill = page.getByRole('group', { name: 'Live at the table' })
  const stop = pill.getByRole('button', { name: 'Stop all', exact: true })
  await expect(stop).toBeInViewport()
  await card.getByRole('button', { name: `Stop ${cue.title}` }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: info.outputPath('phone-cues.png'), animations: 'disabled' })
  await library.getByRole('button', { name: 'Back', exact: true }).click()
  await page.getByRole('navigation', { name: 'Channels' }).getByRole('button', { name: 'Sage' }).click()
  await expect(library).toBeHidden()
  await pill.getByRole('button', { name: '2 live' }).click()
  const soundsAndReveals = pill.getByRole('list', { name: 'Live reveals and sounds' })
  await expect(soundsAndReveals.getByRole('button', { name: `Stop ${cue.title}` })).toBeVisible()
  await expect(soundsAndReveals.getByText(/Ondrey/).first()).toBeVisible()
  await expect(stop).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('phone-sage-live.png'), animations: 'disabled' })
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByRole('dialog', { name: 'Navigation' })).toBeVisible()
  await stop.click()
  await expect(pill).toBeHidden()
  expect(commands).toEqual(['play', 'stop all', 'play', 'stop all'])
  expect(revealWrites).toEqual([{
    path: `${prefix}/reveals/stop`, body: { schema_version: 1, command_id: expect.any(String), scope: 'all' },
  }])
})
