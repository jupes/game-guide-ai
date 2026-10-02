/**
 * 1kg.7.2 PR-1 — the GM's reveal routes, reached through nginx.
 *
 * The E2E stack has no database, so every `/campaigns/...` route answers
 * `503 backend_unavailable`. That is exactly what makes this the right proof of
 * the two things only a real nginx and a real service can show together:
 * nginx routes each reveal path to the SERVICE (a Workbench JSON envelope, never
 * the SPA's HTML fallback), and the guards run in front of the database — the
 * origin check and the one 401. What a Confirm and a Stop do to rows is
 * `service/tests/test_reveals_api.py` and `tests/test_reveal_db.py`.
 *
 * A new file: no existing spec is edited.
 */
import { expect, signIn, test } from './fixtures'
import { request as playwrightRequest } from '@playwright/test'
import { APP_ORIGIN, BASE_URL } from './stack'

/** 22 characters after the prefix, the shape the routes' id check admits. */
const CAMPAIGN = `cmp_${'a'.repeat(22)}`
const DOCUMENT = `doc_${'a'.repeat(22)}`
const SESSION = `ses_${'a'.repeat(22)}`
const COMMAND = 'cmd_2f3a4b5c6d7e8f90'

const CONFIRM = {
  schema_version: 1,
  command_id: COMMAND,
  document_id: DOCUMENT,
  session_id: SESSION,
  reveal_epoch: 0,
  version: 1,
  mask: ['name'],
  audience: { kind: 'table' },
}

const STOP = { schema_version: 1, command_id: COMMAND, scope: 'all' }

interface Envelope {
  detail: { code: string; message: string; retryable: boolean }
}

test.describe('the GM reveal routes through nginx', () => {
  test.beforeEach(async ({ page, accounts }) => {
    await page.goto('/')
    await signIn(page, accounts[0])
  })

  test('a read reaches the service and answers a Workbench envelope that is never cached', async ({ page }) => {
    const response = await page.request.get(`/campaigns/${CAMPAIGN}/reveals`)

    expect(response.status()).toBe(503)
    expect(response.headers()['content-type']).toMatch(/^application\/json/)
    expect(response.headers()['cache-control']).toBe('no-store')
    const body = (await response.json()) as Envelope
    expect(body.detail.code).toBe('backend_unavailable')
    expect(body.detail.retryable).toBe(true)
  })

  test('a Confirm from a foreign origin is refused before anything else, and from this one reaches the service', async ({
    page,
  }) => {
    // Playwright's request context sends no Origin by default, and a request
    // with neither Origin nor Sec-Fetch-Site is "not a browser" and allowed, so
    // the foreign Origin has to be sent for the origin check to be what answers.
    const foreign = await page.request.post(`/campaigns/${CAMPAIGN}/reveals`, {
      data: CONFIRM,
      headers: { Origin: 'https://evil.example' },
    })
    expect(foreign.status()).toBe(403)
    const refused = (await foreign.json()) as Envelope
    expect(refused.detail.code).toBe('forbidden')
    expect(refused.detail.message).toBe("That request didn't come from this application.")

    const own = await page.request.post(`/campaigns/${CAMPAIGN}/reveals`, {
      data: CONFIRM,
      headers: { Origin: APP_ORIGIN },
    })
    expect(own.status()).toBe(503)
    expect(own.headers()['content-type']).toMatch(/^application\/json/)
    expect(((await own.json()) as Envelope).detail.code).toBe('backend_unavailable')
  })

  test('a Stop reaches the service on its own path', async ({ page }) => {
    const response = await page.request.post(`/campaigns/${CAMPAIGN}/reveals/stop`, {
      data: STOP,
      headers: { Origin: APP_ORIGIN },
    })

    expect(response.status()).toBe(503)
    expect(response.headers()['content-type']).toMatch(/^application\/json/)
    expect(response.headers()['cache-control']).toBe('no-store')
    expect(((await response.json()) as Envelope).detail.code).toBe('backend_unavailable')
  })
})

test('a signed-out read is the one 401', async () => {
  const signedOut = await playwrightRequest.newContext({ baseURL: BASE_URL })
  const response = await signedOut.get(`/campaigns/${CAMPAIGN}/reveals`)

  expect(response.status()).toBe(401)
  expect(await response.json()).toEqual({ detail: 'not signed in' })
  await signedOut.dispose()
})
