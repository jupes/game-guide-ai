import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

// An 8 × 8 solid purple image with no metadata.
const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNsYGhgwAaYsIoOWgkAzQgBEJwaof4AAAAASUVORK5CYII=',
  'base64',
)

function fixture(name: string): string {
  const value = process.env[`TABLE_BROWSER_${name}`]
  if (!value) throw new Error(`Missing table integration fixture ${name}`)
  return value
}

test('only the owning GM can load a private image, and deletion revokes it on both services',
  async ({ context, page }) => {
    const writeOrigin = fixture('PLAYER_URL')
    const readOrigin = fixture('SCREEN_URL')
    const campaign = fixture('CAMPAIGN')
    const collection = `/campaigns/${campaign}/assets`
    const signIn = (role: string) => context.addCookies([
      { name: 'gga_session', value: fixture(`${role}_COOKIE`), url: writeOrigin + '/' },
    ])

    await signIn('OWNER')
    const reserved = await context.request.post(writeOrigin + collection, {
      headers: { Origin: writeOrigin },
      data: {
        schema_version: 1,
        campaign_id: campaign,
        command_id: randomBytes(16).toString('base64url'),
        kind: 'image',
        media_type: 'image/png',
        size_bytes: png.length,
        alt: 'Purple square',
      },
    })
    expect(reserved.status(), await reserved.text()).toBe(201)
    const { asset_id: asset } = await reserved.json() as { asset_id: string }
    const assetPath = `${collection}/${asset}`
    const original = readOrigin + assetPath
    const thumbnail = original + '?rendition=thumb'
    const uploaded = await context.request.put(writeOrigin + assetPath + '/bytes', {
      headers: { Origin: writeOrigin, 'Content-Type': 'image/png' },
      data: png,
    })
    expect(uploaded.status(), await uploaded.text()).toBe(200)
    expect(await uploaded.json()).toMatchObject({ state: 'ready', width: 8, height: 8 })

    const image = await page.goto(thumbnail)
    expect(image?.status()).toBe(200)
    expect(image?.headers()).toMatchObject({ 'content-type': 'image/webp', 'cache-control': 'no-store' })
    await expect(page.locator('img')).toHaveJSProperty('naturalWidth', 8)
    const range = await context.request.get(original, { headers: { Range: 'bytes=0-7' } })
    expect(range.status()).toBe(206)
    expect(range.headers()).toMatchObject({ 'content-type': 'image/png', 'cache-control': 'no-store' })
    expect(await range.body()).toEqual(png.subarray(0, 8))

    for (const [role, status] of [['PLAYER', 403], ['OTHER_GM', 404], ['SIGNED_OUT', 401]] as const) {
      if (role === 'SIGNED_OUT') await context.clearCookies()
      else await signIn(role)
      for (const url of [original, thumbnail]) {
        const refused = await context.request.get(url)
        expect(refused.status(), `${role} reads ${url}`).toBe(status)
        expect(refused.headers()['content-type']).toContain('application/json')
      }
    }

    await signIn('OWNER')
    const deleted = await context.request.delete(writeOrigin + assetPath, { headers: { Origin: writeOrigin } })
    expect(deleted.status()).toBe(204)
    for (const url of [writeOrigin + assetPath, original, thumbnail]) {
      expect((await context.request.get(url)).status()).toBe(404)
    }
  })

test('a GM attaches a map, reads it across services and saves the same handout after reload', async ({ context, page }, info) => {
  const origin = fixture('PLAYER_URL')
  const other = fixture('SCREEN_URL')
  const campaign = fixture('CAMPAIGN')
  if (info.project.name === 'workbench-media-desktop') await page.setViewportSize({ width: 1280, height: 960 })
  await context.addCookies([{ name: 'gga_session', value: fixture('OWNER_COOKIE'), url: origin + '/' }])
  const generated: string[] = []
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && (path === '/chat' || path.endsWith('/tool-invocations'))) generated.push(path)
  })

  await page.goto(`${origin}/workspace#campaign=${campaign}`)
  await page.getByRole('button', { name: 'Attach image', exact: true }).click()
  const upload = page.getByRole('dialog', { name: 'Attach image to GM thread' })
  await upload.getByLabel('Use as').selectOption('map')
  const file = fileURLToPath(new URL('./fixtures/tidepool-archive.png', import.meta.url))
  await upload.getByLabel('Image file').setInputFiles(file)
  const alt = 'Map of the lower floor of the Tidepool Archive'
  await upload.getByLabel('Image description').fill(alt)
  const attached = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/attach'))
  await upload.getByRole('button', { name: 'Add to GM thread' }).click()
  const response = await attached
  expect(response.status()).toBe(200)
  const entry = await response.json() as { entry_id: string; asset_id: string; use: string; entry_kind: string }
  expect(entry).toMatchObject({ use: 'map', entry_kind: 'attached_image' })
  const { conversation_id: thread } = response.request().postDataJSON() as { conversation_id: string }

  const expand = page.getByRole('button', { name: 'Expand image', exact: true })
  await expect(expand).toHaveCount(1)
  await expect(page.getByRole('img', { name: alt })).toBeVisible()
  await expand.focus()
  await page.keyboard.press('Enter')
  const viewer = page.getByRole('dialog', { name: 'Map', exact: true })
  await expect(viewer.getByRole('img', { name: alt })).toHaveJSProperty('naturalWidth', 500)
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await page.screenshot({ path: info.outputPath('gm-map-viewer.png'), scale: 'css', animations: 'disabled' })
  await page.keyboard.press('Escape')
  await expect(viewer).toBeHidden()
  await expect(expand).toBeFocused()
  await expect(page.getByRole('button', { name: 'Save as handout' })).toBeVisible()
  await expand.evaluate(button => button.parentElement?.scrollIntoView({ block: 'center' }))
  await page.screenshot({ path: info.outputPath('gm-map-thread.png'), scale: 'css', animations: 'disabled' })

  async function save() {
    const saved = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(`/${entry.entry_id}/promote`))
    await page.getByRole('button', { name: 'Save as handout' }).click()
    const result = await saved
    expect(result.ok()).toBe(true)
    return await result.json() as { document_id: string; data: { portrait: { asset_id: string } } }
  }
  const first = await save()
  expect(first.data.portrait.asset_id).toBe(entry.asset_id)
  await expect(page.getByRole('button', { name: 'Open handout' })).toBeVisible()

  await page.goto(`${other}/workspace#campaign=${campaign}&conversation=${thread}`)
  await expect(expand).toHaveCount(1)
  await expect(page.getByRole('img', { name: alt })).toBeVisible()
  expect((await save()).document_id).toBe(first.document_id)

  const name = `Archive keeper ${info.project.name === 'workbench-media-desktop' ? 'Rowan' : 'Mira'}`
  const documents = `${other}/campaigns/${campaign}/documents`
  const npc = await context.request.post(documents, {
    headers: { Origin: other },
    data: { schema_version: 1, command_id: randomBytes(16).toString('base64url'), campaign_id: campaign,
      type: 'npc', type_version: 1, data: { name } },
  })
  expect(npc.ok()).toBe(true)
  const { document_id: npcId } = await npc.json() as { document_id: string }
  await page.getByRole('button', { name: 'Link to document' }).click()
  await viewer.getByRole('combobox', { name: 'Document', exact: true }).selectOption({ label: name })
  await viewer.getByRole('button', { name: 'Link image', exact: true }).click()
  await expect(viewer.getByRole('status')).toContainText('Image linked to document.')
  const linked = await context.request.get(`${origin}/campaigns/${campaign}/documents/${npcId}`)
  expect(await linked.json()).toMatchObject({ data: { name, portrait: { asset_id: entry.asset_id } } })
  await viewer.getByRole('button', { name: 'Close', exact: true }).click()

  await page.goto(`${origin}/workspace#campaign=${campaign}&conversation=${thread}&document=${npcId}`)
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
  const switchToChat = page.getByRole('button', { name: 'Chat', exact: true })
  if (await switchToChat.isVisible()) await switchToChat.click()
  const newUploads: string[] = []
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith(`/${campaign}/assets`)) newUploads.push(request.url())
  })
  await page.getByRole('button', { name: 'Attach image', exact: true }).click()
  await upload.getByRole('checkbox', { name: 'Use the open document’s image' }).check()
  const reused = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/attach'))
  await upload.getByRole('button', { name: 'Add to GM thread' }).click()
  const portrait = await reused
  expect(portrait.status()).toBe(200)
  expect(await portrait.json()).toMatchObject({ entry_kind: 'attached_image', use: 'portrait', asset_id: entry.asset_id })
  await expect(expand).toHaveCount(2)
  expect(newUploads).toEqual([])
  expect(generated).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
