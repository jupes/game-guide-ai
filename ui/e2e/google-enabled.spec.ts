import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'
import { expect, signIn, test } from './fixtures'

async function chooseGoogleAccount(page: Page, account: string): Promise<void> {
  await expect(page).toHaveURL(/\/auth\/google\/_e2e\/authorize\?/)
  await page.getByRole('textbox', { name: 'Test account' }).fill(account)
  await page.getByRole('button', { name: 'Choose account' }).click()
}

test('an invited player creates an account and later signs in with the same Google identity', async ({ page }) => {
  const account = randomUUID()
  await page.goto(`/#invite=e2e-invite-${randomUUID()}`)
  await page.getByRole('button', { name: 'Sign up with Google' }).click()
  await chooseGoogleAccount(page, account)

  const enterTavern = page.getByRole('button', { name: 'Enter the Tavern' })
  await expect(enterTavern).toBeVisible()
  const created = await (await page.request.get('/auth/me')).json()
  expect(created.email).toBe(`${account}@example.com`)

  expect((await page.request.post('/auth/logout')).ok()).toBe(true)
  await page.goto('/')
  await page.getByRole('link', { name: 'Sign in with Google' }).click()
  await chooseGoogleAccount(page, account)
  await expect(enterTavern).toBeVisible()
  const signedIn = await (await page.request.get('/auth/me')).json()
  expect(signedIn).toEqual(created)
})

test('a password account links and unlinks a Google identity from Profile', async ({ page, accounts }) => {
  const [passwordAccount] = accounts
  const googleAccount = randomUUID()
  await page.goto('/')
  await signIn(page, passwordAccount)
  await page.goto('/profile')
  const section = page.getByRole('region', { name: 'Google account' })
  const password = section.getByLabel('Current password')
  await password.fill(passwordAccount.password)
  await section.getByRole('button', { name: 'Continue with Google' }).click()
  await chooseGoogleAccount(page, googleAccount)

  await expect(page.getByRole('status')).toHaveText('Google account linked. You can now sign in with Google.')
  await expect(section).toContainText(`Linked to ${googleAccount}@example.com`)
  await password.fill(passwordAccount.password)
  await section.getByRole('button', { name: 'Unlink Google' }).click()
  await expect(page.getByRole('status')).toHaveText('Google account unlinked. Sign in with your password.')
  await expect(section.getByRole('button', { name: 'Continue with Google' })).toBeVisible()
  expect((await (await page.request.get('/auth/me')).json()).email).toBe(passwordAccount.email)
})
