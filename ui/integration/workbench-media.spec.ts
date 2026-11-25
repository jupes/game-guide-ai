import { randomBytes } from 'node:crypto'
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
