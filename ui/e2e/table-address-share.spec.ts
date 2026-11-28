import type { Locator } from '@playwright/test'
import jsQR from 'jsqr'
import { expect, signIn, test } from './fixtures'
import { installRevealRoutes } from './revealStubs'
import { CAMPAIGN_ID, DOCUMENT_ID, DOCUMENT_TITLE, installWorkbenchRoutes, THREAD_PAGE } from './workbenchStubs'

async function scan(image: Locator): Promise<string | undefined> {
  const png = await image.screenshot({ animations: 'disabled', scale: 'css' })
  const raster = await image.page().evaluate(async (base64) => {
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0))
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const context = canvas.getContext('2d')!
    context.drawImage(bitmap, 0, 0)
    bitmap.close()
    const { data, width, height } = context.getImageData(0, 0, canvas.width, canvas.height)
    return { rgba: Array.from(data), width, height }
  }, png.toString('base64'))
  return jsQR(Uint8ClampedArray.from(raster.rgba), raster.width, raster.height)?.data
}

test('the GM shares a scannable table address that still requires table access', async ({
  page, browser, baseURL, accounts, isMobile,
}, info) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  await installWorkbenchRoutes(page)
  await installRevealRoutes(page)
  await page.route(url => url.pathname === '/conversations' && !url.searchParams.has('campaign_id'),
    route => route.fulfill({ json: THREAD_PAGE }))
  await page.addInitScript(() => {
    // A phone can decline clipboard access. Capture the copy request, then exercise that refusal too.
    let copies = 0
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      async writeText(value: string) {
        if (copies++) throw new DOMException('Clipboard unavailable', 'NotAllowedError')
        Reflect.set(window, 'copiedTableAddress', value)
      },
    } })
  })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}&document=${DOCUMENT_ID}`)
  const share = page.getByRole('button', { name: 'Share table', exact: true })
  const address = page.getByRole('textbox', { name: 'Table address' })
  const copy = page.getByRole('button', { name: 'Copy link', exact: true })
  await share.click()
  const expected = new URL(`/table?campaign_id=${CAMPAIGN_ID}`, page.url()).href
  await expect(address).toHaveValue(expected)
  await expect(address).toHaveAttribute('readonly', '')
  await page.getByRole('button', { name: 'Show QR code', exact: true }).click()
  const image = page.getByRole('img', { name: 'QR code for the player table', exact: true })
  await image.scrollIntoViewIfNeeded()
  // Allow subpixel intersection rounding at emulated device scale.
  await expect(image).toBeInViewport({ ratio: 0.995 })
  const decoded = await scan(image)
  expect(decoded).toBe(expected)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: info.outputPath('table-address-qr.png'), animations: 'disabled', scale: 'css' })

  if (!isMobile) {
    for (const viewport of [{ width: 320, height: 568 }, { width: 844, height: 390 }]) {
      await page.setViewportSize(viewport)
      await image.scrollIntoViewIfNeeded()
      await expect(image).toBeInViewport({ ratio: 0.995 })
      const hiddenScroller = await image.evaluate(el => {
        for (let parent = el.parentElement; parent; parent = parent.parentElement) {
          if (parent.scrollTop > 1 && getComputedStyle(parent).overflowY === 'hidden') return parent.className
        }
        return null
      })
      expect(hiddenScroller, 'the QR must be reachable without scrolling a clipped container by script').toBeNull()
      expect(await scan(image)).toBe(expected)
      await copy.scrollIntoViewIfNeeded()
      await expect(copy).toBeInViewport({ ratio: 0.995 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    }
  }
  await page.getByRole('button', { name: 'Hide QR code', exact: true }).click()
  await expect(image).toBeHidden()
  await expect(address).toHaveValue(expected)
  await copy.click()
  await expect.poll(() => page.evaluate(() => Reflect.get(window, 'copiedTableAddress'))).toBe(expected)
  const disclosure = page.locator('.table-address-share')
  await expect(disclosure.getByText('Table link copied.', { exact: true })).toBeVisible()
  await copy.focus()
  await copy.press('Enter')
  await expect(disclosure.getByText('Select the table address and copy it.', { exact: true })).toBeVisible()
  await expect(address).toBeFocused()
  const selected = await address.evaluate((field: HTMLTextAreaElement) =>
    field.value.substring(field.selectionStart, field.selectionEnd))
  expect(selected).toBe(expected)
  await address.press('Escape')
  await expect(share).toBeFocused()
  if (isMobile) {
    await page.getByRole('button', { name: 'Reveal to party' }).click()
    const sheet = page.getByRole('dialog', { name: `Reveal ${DOCUMENT_TITLE}` })
    const sheetShare = sheet.getByRole('button', { name: 'Share table', exact: true })
    const collapsedHeight = await sheet.locator('.table-address-share').evaluate(el => el.getBoundingClientRect().height)
    expect(collapsedHeight).toBeLessThanOrEqual(64)
    await sheetShare.click()
    await expect(sheet.getByRole('textbox', { name: 'Table address' })).toHaveValue(expected)
    await sheet.getByRole('button', { name: 'Show QR code', exact: true }).press('Escape')
    await expect(sheet).toBeVisible()
    await expect(sheetShare).toBeFocused()
  }

  // A fresh browser follows the scanned URL with neither GM credentials nor a screen grant.
  const guest = await browser.newContext({ baseURL })
  try {
    const table = await guest.newPage()
    await table.goto(decoded!)
    await expect(table.getByText('Sign in to open this table.', { exact: true })).toBeVisible()
    const snapshot = await guest.request.get(`/table/snapshot?campaign_id=${CAMPAIGN_ID}`)
    expect(snapshot.status()).toBe(401)
  } finally {
    await guest.close()
  }
})
