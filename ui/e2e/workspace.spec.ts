/**
 * Two workspace surfaces the single happy path never touched: the model picker
 * and the profile page.
 */

import { expect, signIn, test } from './fixtures'

test('the model picker is bound to a conversation, and changing it after the first prompt starts a new one', async ({
  page,
  accounts,
}) => {
  const prompt = 'What is the range of a shortbow?'

  await page.goto('/')
  await signIn(page, accounts[0])
  await page.getByRole('button', { name: 'Sage' }).click()

  const model = page.getByRole('combobox', { name: 'Model' })
  // Nothing to bind a preference to yet.
  await expect(model).toBeDisabled()

  await page.getByRole('button', { name: 'New conversation' }).click()
  await expect(model).toBeEnabled()
  await expect(model).toHaveValue('auto')

  // Before the first prompt the server has committed to nothing, so the
  // preference changes in place.
  await model.selectOption({ label: 'GPT-4o mini' })
  await expect(model).toHaveValue('gpt-4o-mini')

  await page.getByPlaceholder('Ask…').fill(prompt)
  await page.getByRole('button', { name: 'Send message' }).click()
  await expect(page.getByText(`E2E sage answer: ${prompt}`)).toBeVisible()

  // After the first prompt the conversation's routing is bound. Changing the
  // model must ASK and then start a fresh conversation — never silently
  // re-point a conversation the server has already committed to.
  page.once('dialog', (dialog) => {
    expect(dialog.type()).toBe('confirm')
    void dialog.accept()
  })
  await model.selectOption({ label: 'Automatic' })
  await expect(model).toHaveValue('auto')

  // A NEW conversation: empty thread, with the bound one still in the list.
  await expect(page.getByText('Ask the Sage…')).toBeVisible()
  await expect(page.getByText(`E2E sage answer: ${prompt}`)).toHaveCount(0)
  await expect(page.getByRole('button', { name: prompt, exact: true })).toBeVisible()
})

test('the profile page shows the role the server assigned and keeps an edited display name across a reload', async ({
  page,
  accounts,
}) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  await page.getByRole('button', { name: 'Sage' }).click()

  await page.getByRole('button', { name: 'Open user menu' }).click()
  // Server-authoritative (the invite that created the account carried the DM
  // role); the control is read-only and must still reflect it.
  await expect(page.getByRole('switch', { name: 'Dungeon Master role' })).toBeChecked()
  await expect(
    page.getByRole('switch', { name: 'Dungeon Master role' }),
  ).toBeDisabled()

  await page.getByRole('menuitem', { name: 'Profile' }).click()
  await expect(page.getByRole('heading', { name: 'Profile' })).toBeVisible()

  const displayName = page.getByRole('textbox', { name: 'Display name' })
  await expect(displayName).toHaveValue('Adventurer')
  await displayName.fill('Tessa Quill')

  await page.getByRole('button', { name: 'Arcane avatar' }).click()
  await expect(page.getByRole('button', { name: 'Arcane avatar' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )

  await page.getByRole('button', { name: 'Back to chat' }).click()
  await expect(page.getByRole('navigation', { name: 'Channels' })).toBeVisible()

  // Survives a reload: the edit was persisted for this account, not just held
  // in the component that made it.
  await page.reload()
  await page.getByRole('button', { name: 'Sage' }).click()
  await page.getByRole('button', { name: 'Open user menu' }).click()
  await page.getByRole('menuitem', { name: 'Profile' }).click()
  await expect(displayName).toHaveValue('Tessa Quill')
  await expect(page.getByRole('button', { name: 'Arcane avatar' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})
