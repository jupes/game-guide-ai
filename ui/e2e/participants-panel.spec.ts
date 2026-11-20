import { expect, test } from './fixtures'
import { CAMPAIGN_ID, installWorkbenchRoutes } from './workbenchStubs'
import { SeatSchema, type Group, type Seat, type SeatStatus } from '../src/gm/contracts'

const CAMPAIGN = `/campaigns/${CAMPAIGN_ID}`
const SEATS = `${CAMPAIGN}/participants`
const SHEET = 'doc_e2eCharacterSheet00000001'
const PERSON = 'prt_e2eParticipants000000001'
const CREATED = 'prt_e2eParticipants000000002'
const GROUP = 'grp_e2eParticipants000000001'
const NOW = '2026-09-16T19:20:11Z'

function seat(id: string, alias: string, status: SeatStatus, address: string | null = null): Seat {
  return SeatSchema.parse({
    schema_version: 1, participant_id: id, alias, status, address, created_at: NOW,
    offered_at: null, offer_expires_at: null, accepted_at: null, confirmed_at: null, removed_at: null,
  })
}

test('on a phone a GM can recover an offer, confirm a participant, manage a character sheet, and remove a seat', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.addInitScript(() => localStorage.setItem('aetheril-theme', 'dark'))
  await page.route('**/auth/me', (route) => route.fulfill({ json: { email: 'gm-e2e@example.com', role: 'dm' } }))
  await installWorkbenchRoutes(page)
  await page.route('**/models', (route) => route.fulfill({ json: { default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] } }))
  await page.route((url) => /\/(reveals|table-session|tool-availability)$/.test(url.pathname),
    (route) => route.fulfill({ status: 404, json: {} }))
  await page.route('**/metrics/ui', (route) => route.fulfill({ status: 204 }))

  let seats = [seat(PERSON, 'Ada', 'awaiting_confirmation', 'ada@example.com')]
  let link: string | null = null
  let failRead = true
  let offers = 0
  let group: Group | null = null
  const createGroupKeys: string[] = []
  const writes: string[] = []
  await page.route((url) => url.pathname.startsWith(SEATS), (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'GET') {
      if (failRead) return route.fulfill({ status: 503, json: {} })
      return route.fulfill({ json: { schema_version: 1, items: seats, next_cursor: null } })
    }
    writes.push(path)
    const body = route.request().postDataJSON()
    if (route.request().method() === 'PATCH') {
      if (seats.some((item) => item.participant_id !== PERSON && item.alias === body.alias)) {
        return route.fulfill({
          status: 409, json: { detail: { code: 'alias_taken', message: 'Display name already in use.', retryable: false } },
        })
      }
      const updated = { ...seats[0], alias: body.alias }
      seats = [updated, ...seats.slice(1)]
      return route.fulfill({ json: updated })
    }
    if (path === SEATS) {
      const newSeat = seat(CREATED, body.alias, 'open')
      seats = [...seats, newSeat]
      return route.fulfill({ status: 201, json: newSeat })
    }
    if (path.endsWith('/offer')) {
      if (++offers === 1) return route.fulfill({ status: 503, json: {} })
      seats = [seats[0], seat(CREATED, seats[1].alias, 'offered', body.email)]
      return route.fulfill({ status: 204 })
    }
    if (path.endsWith('/confirm')) {
      const confirmed = seat(PERSON, 'Ada', 'confirmed', 'ada@example.com')
      seats = [confirmed, ...seats.slice(1)]
      return route.fulfill({ json: confirmed })
    }
    if (body.password !== 'test-only-password') {
      return route.fulfill({ status: 403, json: { detail: { code: 'reauth_failed', message: 'Try again', retryable: true } } })
    }
    seats = seats.filter((item) => item.participant_id !== PERSON)
    link = null
    return route.fulfill({ status: 204 })
  })
  await page.route((url) => url.pathname === `${CAMPAIGN}/library`, (route) => {
    expect(route.request().postDataJSON()).toMatchObject({ category: 'documents', type: 'character-sheet' })
    return route.fulfill({ json: {
      schema_version: 1, campaign_id: CAMPAIGN_ID, category: 'documents', next_cursor: null,
      items: [{ document_id: SHEET, type: 'character-sheet', title: 'Rowan', qualifier: '', tags: [], archived: false, updated_at: NOW }],
    } })
  })
  await page.route((url) => url.pathname.startsWith(`${CAMPAIGN}/documents/${SHEET}/`), (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: { schema_version: 1, document_id: SHEET, participant_id: link, seat_active: link !== null } })
    }
    writes.push(path)
    link = path.endsWith('/unlink') ? null : PERSON
    return route.fulfill({ status: 204 })
  })
  const groups = `${CAMPAIGN}/groups`
  await page.route((url) => url.pathname.startsWith(groups), (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET') {
      return route.fulfill({ json: { schema_version: 1, items: group === null ? [] : [group], next_cursor: null } })
    }
    if (path === groups) {
      const body = request.postDataJSON()
      createGroupKeys.push(body.command_id)
      if (group !== null) return route.fulfill({ status: 201, json: group })
      group = { schema_version: 1, group_id: GROUP, name: body.name, member_ids: [], created_at: NOW, updated_at: NOW }
      return route.fulfill({ status: 503, json: {} })
    }
    if (group === null) return route.fulfill({ status: 404, json: {} })
    if (request.method() === 'PATCH') {
      group = { ...group, name: request.postDataJSON().name }
      return route.fulfill({ json: group })
    }
    if (path.endsWith(`/members/${CREATED}`)) group = { ...group, member_ids: [CREATED] }
    else if (path.endsWith(`/members/${CREATED}/remove`)) group = { ...group, member_ids: [] }
    else if (path.endsWith(`/${GROUP}/remove`)) group = null
    return route.fulfill({ status: 204 })
  })

  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}`)
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await page.getByRole('button', { name: 'Participants', exact: true }).click()
  const panel = page.getByRole('region', { name: 'Participants' })
  const heading = panel.getByRole('heading', { name: 'Participants' })
  await expect(heading).toBeFocused()
  await expect(panel.getByText("Couldn't load participants.")).toBeVisible()
  failRead = false
  await panel.getByRole('button', { name: 'Retry', exact: true }).click()

  await panel.getByRole('button', { name: 'Offer a seat', exact: true }).click()
  const offer = page.getByRole('dialog', { name: 'Offer a seat' })
  await expect(offer.getByRole('textbox', { name: 'Display name' })).toBeFocused()
  await expect(page.locator('.workspace-shell__body')).toHaveAttribute('inert', '')
  await offer.getByRole('textbox', { name: 'Display name' }).fill('Bren')
  await offer.getByRole('textbox', { name: 'Player email' }).fill('bren@example.com')
  await offer.getByRole('button', { name: 'Send offer' }).click()
  await expect(offer.getByRole('alert')).toContainText('The seat is saved.')
  await offer.getByRole('button', { name: 'Send offer' }).click()
  await expect(panel.getByText('Offer pending')).toBeVisible()
  expect(writes.filter((path) => path === SEATS)).toHaveLength(1)
  expect(writes.filter((path) => path.endsWith('/offer'))).toEqual(Array(2).fill(`${SEATS}/${CREATED}/offer`))

  await panel.getByRole('button', { name: 'Confirm Ada' }).click()
  const confirm = page.getByRole('dialog', { name: 'Confirm Ada' })
  await expect(confirm.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await confirm.getByRole('button', { name: 'Confirm seat' }).click()
  await expect(panel.getByText('Seated', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: 'Character sheets' }).click()
  await panel.getByRole('combobox', { name: 'Character sheet', exact: true }).selectOption({ label: 'Rowan' })
  await panel.getByRole('combobox', { name: 'Link to participant', exact: true }).selectOption({ label: 'Ada' })
  await panel.getByRole('button', { name: 'Link sheet', exact: true }).click()
  await expect(panel.getByText('Linked to Ada.')).toBeVisible()
  await panel.getByRole('button', { name: 'Rename Ada' }).click()
  const rename = page.getByRole('dialog', { name: 'Rename Ada' })
  const displayName = rename.getByRole('textbox', { name: 'Display name' })
  await expect(displayName).toHaveValue('Ada')
  await displayName.fill('Bren')
  await rename.getByRole('button', { name: 'Save name' }).click()
  await expect(rename.getByRole('alert')).toContainText('already uses that display name')
  await displayName.fill('Ada Marsh')
  await rename.getByRole('button', { name: 'Save name' }).click()
  await expect(panel.getByText('Linked to Ada Marsh.')).toBeVisible()
  await panel.getByRole('button', { name: 'Unlink sheet' }).click()
  await expect(panel.getByText('Character sheet unlinked.')).toBeVisible()

  await panel.getByRole('button', { name: 'Remove Ada Marsh' }).click()
  const remove = page.getByRole('dialog', { name: 'Remove Ada Marsh' })
  const password = remove.getByLabel('Your password')
  await password.fill('wrong-test-password')
  await remove.getByRole('button', { name: 'Remove seat' }).click()
  await expect(remove.getByRole('alert')).toContainText('That password did not match.')
  await expect(password).toHaveValue('')
  await password.fill('test-only-password')
  await remove.getByRole('button', { name: 'Remove seat' }).click()
  await expect(panel.getByRole('heading', { name: 'Ada Marsh' })).toBeHidden()
  await expect(heading).toBeFocused()
  await expect(panel.getByRole('heading', { name: 'Bren' })).toBeVisible()
  await panel.getByRole('button', { name: 'Groups', exact: true }).click()
  await panel.getByRole('button', { name: 'New group' }).click()
  const createGroup = page.getByRole('dialog', { name: 'New group' })
  const name = createGroup.getByRole('textbox', { name: 'Group name' })
  await expect(name).toBeFocused()
  await name.fill('Scouts')
  await name.press('Enter')
  await expect(createGroup.getByRole('alert')).toContainText('Try again')
  await createGroup.getByRole('button', { name: 'Save group' }).click()
  await expect(panel.getByRole('heading', { name: 'Scouts' })).toHaveCount(1)
  expect(createGroupKeys).toHaveLength(2)
  expect(createGroupKeys[1]).toBe(createGroupKeys[0])

  await panel.getByRole('button', { name: 'Rename group Scouts' }).click()
  const renameGroup = page.getByRole('dialog', { name: 'Rename Scouts' })
  await renameGroup.getByRole('textbox', { name: 'Group name' }).fill('Pathfinders')
  await renameGroup.getByRole('button', { name: 'Save group' }).click()
  await panel.getByRole('button', { name: 'Add member to Pathfinders' }).click()
  const addMember = page.getByRole('dialog', { name: 'Add a member to Pathfinders' })
  await addMember.getByRole('combobox', { name: 'Participant' }).selectOption({ label: 'Bren' })
  await addMember.getByRole('button', { name: 'Add member' }).click()
  await expect(panel.getByRole('list', { name: 'Members of Pathfinders' })).toContainText('Bren (unconfirmed)')
  await panel.getByRole('button', { name: 'Remove Bren from Pathfinders' }).click()
  const removeMember = page.getByRole('dialog', { name: 'Remove Bren from Pathfinders' })
  await expect(removeMember.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await removeMember.getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(panel.getByText('No members yet.')).toBeVisible()
  await panel.getByRole('button', { name: 'Remove group Pathfinders' }).click()
  await page.getByRole('dialog', { name: 'Remove Pathfinders' }).getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(panel.getByText('No groups yet.')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await panel.getByRole('button', { name: 'Back' }).click()
  await expect(panel).toBeHidden()
})

test('a player does not get Participants controls from a campaign link', async ({ page }) => {
  let seatReads = 0
  await page.route('**/auth/me', (route) => route.fulfill({
    json: { email: 'player-e2e@example.com', role: 'player' },
  }))
  await page.route('**/models', (route) => route.fulfill({ json: { default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] } }))
  await page.route((url) => url.pathname === SEATS, (route) => {
    seatReads += 1
    return route.fulfill({ status: 403, json: {} })
  })
  await page.goto(`/workspace#campaign=${CAMPAIGN_ID}`)
  await expect(page).not.toHaveURL(/campaign=/)
  await expect(page.getByRole('button', { name: 'Participants', exact: true })).toHaveCount(0)
  expect(seatReads).toBe(0)
})
