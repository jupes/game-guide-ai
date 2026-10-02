/**
 * 1kg.7.2 PR-2 — the table's snapshot read, reached through nginx.
 *
 * The E2E stack has no database, so an entitled read has nothing to read and
 * answers `503 backend_unavailable`. That is what makes this the right proof of
 * the two things only a real nginx and a real service can show together: nginx
 * names `/table/snapshot` and routes it to the SERVICE (a Workbench JSON
 * envelope with the table headers, never the SPA's HTML fallback), and the
 * guards run in front of the database — Fetch Metadata, the one 401 and the
 * one `inactive`. What a read returns is `service/tests/test_table_api.py` and
 * `tests/test_table_snapshot_db.py`.
 *
 * A new file: no existing spec is edited.
 */
import { expect, signIn, test } from './fixtures'
import { request as playwrightRequest } from '@playwright/test'
import { BASE_URL } from './stack'

/** 22 characters after the prefix, the shape the route's id check admits. */
const CAMPAIGN = `cmp_${'a'.repeat(22)}`

interface Envelope {
  detail: { code: string; message: string; retryable: boolean }
}

test.describe('the table snapshot through nginx', () => {
  test.beforeEach(async ({ page, accounts }) => {
    await page.goto('/')
    await signIn(page, accounts[0])
  })

  test('a read reaches the service and answers an envelope with the table headers', async ({ page }) => {
    const response = await page.request.get(`/table/snapshot?campaign_id=${CAMPAIGN}`)

    expect(response.status()).toBe(503)
    expect(response.headers()['content-type']).toMatch(/^application\/json/)
    expect(response.headers()['cache-control']).toBe('no-store')
    expect(response.headers()['cross-origin-resource-policy']).toBe('same-origin')
    const body = (await response.json()) as Envelope
    expect(body.detail.code).toBe('backend_unavailable')
    expect(body.detail.retryable).toBe(true)
  })

  test('a cross-site read is refused before any cookie or row is read', async ({ page }) => {
    // Sec-Fetch-Site is a header a browser sets and a page cannot; the request
    // context is Node, so it can send what a cross-site navigation would.
    const response = await page.request.get(`/table/snapshot?campaign_id=${CAMPAIGN}`, {
      headers: { 'Sec-Fetch-Site': 'cross-site' },
    })

    expect(response.status()).toBe(403)
    expect(((await response.json()) as Envelope).detail.code).toBe('cross_site')
  })

  test('a malformed campaign is the one inactive, with no database needed', async ({ page }) => {
    const response = await page.request.get('/table/snapshot?campaign_id=not-an-id')

    expect(response.status()).toBe(404)
    expect(response.headers()['cache-control']).toBe('no-store')
    expect(((await response.json()) as Envelope).detail.code).toBe('inactive')
  })
})

test('a signed-out read is the one 401', async () => {
  const signedOut = await playwrightRequest.newContext({ baseURL: BASE_URL })
  const response = await signedOut.get(`/table/snapshot?campaign_id=${CAMPAIGN}`)

  expect(response.status()).toBe(401)
  expect(await response.json()).toEqual({ detail: 'not signed in' })
  await signedOut.dispose()
})
