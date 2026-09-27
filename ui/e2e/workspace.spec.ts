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
  //
  // Dialogs are COLLECTED, not asserted on inside the listener. An
  // `expect` in a `page.once('dialog', …)` handler runs only if a dialog is
  // actually raised, so the one regression this exists to catch — the app
  // dropping the prompt and forking (or re-pointing) in silence — is the one
  // case it would stay quiet for. Asserting on the collected array afterwards
  // makes the ABSENCE of the dialog the failure.
  const dialogs: { type: string; message: string }[] = []
  page.on('dialog', (dialog) => {
    dialogs.push({ type: dialog.type(), message: dialog.message() })
    void dialog.accept()
  })
  await model.selectOption({ label: 'Automatic' })
  await expect(model).toHaveValue('auto')

  // Exactly one prompt, it is a confirm (cancellable — an alert would tell
  // rather than ask), and it names the model it is about to switch to, because
  // "OK" to an unlabelled question is a guess.
  expect(dialogs.map((dialog) => dialog.type)).toEqual(['confirm'])
  expect(dialogs[0]?.message).toContain('Automatic')

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

  // agent-forge-harness-3j4: labelled group of buttons, not an ARIA menu —
  // scoped + exact (Playwright's `name` is otherwise a substring match, and
  // the LeftNav lists conversations as buttons titled by the user's prompts).
  await page
    .getByRole('group', { name: 'User menu', exact: true })
    .getByRole('button', { name: 'Profile', exact: true })
    .click()
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
  await page
    .getByRole('group', { name: 'User menu', exact: true })
    .getByRole('button', { name: 'Profile', exact: true })
    .click()
  await expect(displayName).toHaveValue('Tessa Quill')
  await expect(page.getByRole('button', { name: 'Arcane avatar' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
})
