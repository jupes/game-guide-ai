/**
 * The `test` every spec in this directory imports.
 *
 * It is the Playwright test with three guards that apply to EVERY spec whether
 * or not the spec mentions them, plus the accounts the signed-in flows need.
 *
 * The guards are automatic on purpose: they are the kind of regression nobody
 * writes a dedicated test for, and a guard you have to remember to opt into is
 * a guard that is missing from the next spec somebody adds.
 */

import { expect, test as base, type Page } from '@playwright/test'
import { APP_ORIGIN, BASE_URL } from './stack'

// ── Accounts ─────────────────────────────────────────────────────────────────

export interface TesterAccount {
  email: string
  password: string
}

/** Long enough for the service's own minimum (credentials.ts MIN_PASSWORD_LENGTH). */
const TESTER_PASSWORD = 'e2e-password-123'

/** Two accounts, because one of the things worth proving is that the SECOND
 * person to sign in on a browser does not inherit the first one's state. */
const ACCOUNT_SLOTS = [0, 1] as const

/**
 * Invites are single-use and seeded by name in service/e2e_app.py, so the token
 * has to be predictable AND unused. `workerIndex` is unique per worker PROCESS
 * — a worker that is restarted after a failure gets a fresh one — which is
 * exactly the grain at which this fixture runs.
 */
function inviteToken(workerIndex: number, slot: number): string {
  return `e2e-invite-account-${workerIndex}-${slot}`
}

// ── Guards ───────────────────────────────────────────────────────────────────

interface RejectionReporter {
  __e2eReportUnhandledRejection?: (reason: string) => void
}

/** Requests the page made to an origin that is not the application's.
 *
 * The suite used to watch for `fonts.googleapis.com` / `fonts.gstatic.com`
 * only. The property actually worth holding is broader and simpler: the
 * production build self-hosts everything, so a page load that reaches ANY
 * other origin is a regression — a re-introduced CDN, an analytics beacon, a
 * tracking pixel — and it is also a privacy and offline-behaviour claim.
 */
function watchForRemoteSubresources(page: Page, found: string[]): void {
  page.on('request', (request) => {
    const url = new URL(request.url())
    // data:, blob: and about: are in-document, not network fetches.
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return
    if (url.origin === APP_ORIGIN) return
    found.push(request.url())
  })
}

interface Guards {
  /** Automatic; never referenced by a spec. */
  guards: void
}

interface Accounts {
  /** Two signed-up accounts, shared by every test in this worker. */
  accounts: readonly [TesterAccount, TesterAccount]
}

export const test = base.extend<Guards, Accounts>({
  guards: [
    async ({ page }, use) => {
      const remoteRequests: string[] = []
      const pageErrors: string[] = []
      const unhandledRejections: string[] = []

      watchForRemoteSubresources(page, remoteRequests)
      page.on('pageerror', (error) => pageErrors.push(String(error)))

      // Reported OUT of the page rather than read back at teardown: the array
      // would otherwise be wiped by every reload, and a teardown-time evaluate
      // races with whatever navigation the test ended on. The binding is
      // re-installed on each navigation, so it survives page.reload().
      await page.exposeFunction(
        '__e2eReportUnhandledRejection',
        (reason: string) => {
          unhandledRejections.push(reason)
        },
      )
      await page.addInitScript(() => {
        window.addEventListener('unhandledrejection', (event) => {
          ;(window as typeof window & RejectionReporter)
            .__e2eReportUnhandledRejection?.(String(event.reason))
        })
      })

      await use()

      expect(
        remoteRequests,
        'the app must not load a subresource from any remote origin',
      ).toEqual([])
      expect(pageErrors, 'the page must not throw').toEqual([])
      expect(
        unhandledRejections,
        'the page must not leave a promise rejection unhandled',
      ).toEqual([])
    },
    { auto: true },
  ],

  accounts: [
    async ({ playwright }, use, workerInfo) => {
      // Provisioned over the API, not through the UI: these accounts exist so
      // the flows BELOW sign-up have something to sign in as. Creating them
      // through the form would spend an extra invite per test and make every
      // spec depend on the signup screen it is not testing (app.spec.ts covers
      // signup through the browser, which is where that belongs).
      const request = await playwright.request.newContext({ baseURL: BASE_URL })
      const created: TesterAccount[] = []
      for (const slot of ACCOUNT_SLOTS) {
        const account: TesterAccount = {
          email: `e2e-account-${workerInfo.workerIndex}-${slot}@example.com`,
          password: TESTER_PASSWORD,
        }
        const response = await request.post('/auth/signup', {
          data: {
            email: account.email,
            password: account.password,
            invite: inviteToken(workerInfo.workerIndex, slot),
          },
        })
        expect(
          response.status(),
          `seeding ${account.email} (invite ${inviteToken(workerInfo.workerIndex, slot)})`,
        ).toBe(200)
        created.push(account)
      }
      await request.dispose()
      await use([created[0], created[1]] as const)
    },
    { scope: 'worker' },
  ],
})

export { expect } from '@playwright/test'

// ── Shared steps ─────────────────────────────────────────────────────────────

/**
 * Sign in through the form, from a page already showing the sign-in screen.
 * Resolves once the app has adopted the session and rendered the landing
 * screen — so callers never have to wait for anything themselves.
 */
export async function signIn(
  page: Page,
  account: TesterAccount,
): Promise<void> {
  await page.getByLabel('Email').fill(account.email)
  await page.getByLabel('Password').fill(account.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('button', { name: 'Enter the Tavern' })).toBeVisible()
}
