import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'
import { CAMPAIGN_ID, DOCUMENT_ID, DOCUMENT_TITLE, installWorkbenchRoutes } from './workbenchStubs'
import { TOOL_IDS, type ToolInvocationRequest } from '../src/gm/contracts'
import { cardResult, documentResult, toolInvocation } from '../src/gm/laneFixtures'

const THREAD = 'cnv_e2eWorkbenchThread00000001'
const ENTRY = 'entry_e2eWorkbenchTool00000001'
const WORKSPACE = `/workspace#campaign=${CAMPAIGN_ID}&conversation=${THREAD}`
const thread = {
  schema_version: 1, conversation_id: THREAD, campaign_id: CAMPAIGN_ID, started_mode: 'gm',
  title: null, created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
}

async function prepWorkbench(page: Page): Promise<void> {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.route('**/auth/me', (route) => route.fulfill({
    json: { email: 'composer-e2e@example.com', role: 'dm' },
  }))
  await installWorkbenchRoutes(page)
  await page.route('**/models', (route) => route.fulfill({ json: {
    default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }],
  } }))
  await page.route((url) => /\/(reveals|table-session|attachments)$/.test(url.pathname),
    (route) => route.fulfill({ status: 404, json: {} }))
  await page.route('**/metrics/ui', (route) => route.fulfill({ status: 204 }))
  await page.route((url) => url.pathname === `/conversations/${THREAD}`, (route) => route.fulfill({ json: thread }))
}

test('a phone keeps the GM draft, runs from Send, and restores the result after reload', async ({ page }) => {
  await prepWorkbench(page)
  const calls: ToolInvocationRequest[] = []
  const invocation = (request: ToolInvocationRequest) => toolInvocation({
    invocation_id: request.invocation_id,
    status: 'done',
    result: documentResult({ document: {
      title: DOCUMENT_TITLE, document_id: DOCUMENT_ID, type: 'npc', library_category: 'npcs',
    } }),
  })
  await page.route((url) => url.pathname === `/conversations/${THREAD}/timeline`, (route) => route.fulfill({ json: {
    schema_version: 1, conversation_id: THREAD, next_cursor: null,
    items: calls.map((request) => ({
      schema_version: 1, entry_id: ENTRY, entry_kind: 'tool', created_at: '2026-09-18T12:00:00Z',
      brief: request.brief, invocation: invocation(request),
    })),
  } }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/tool-availability`, (route) => route.fulfill({
    json: Object.fromEntries(TOOL_IDS.map((id) => [id, id === 'npc' ? null : 'Unavailable in this test.'])),
  }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/tool-invocations`, (route) => {
    const request = route.request().postDataJSON() as ToolInvocationRequest
    calls.push(request)
    return route.fulfill({ json: invocation(request), headers: { 'X-Workbench-Entry-Id': ENTRY } })
  })

  await page.goto(WORKSPACE)
  const rail = page.getByRole('group', { name: 'GM tools' })
  const more = rail.getByRole('button', { name: 'More', exact: true })
  await more.click()
  await page.getByRole('menuitem', { name: 'Customise rail' }).click()
  const dialog = page.getByRole('dialog', { name: 'Customise rail' })
  await expect(dialog).toBeFocused()
  await expect(page.locator('.workspace-shell__body')).toHaveAttribute('inert', '')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(more).toBeFocused()

  await rail.getByRole('button', { name: 'NPC', exact: true }).click()
  const field = page.getByRole('textbox', { name: 'Message' })
  await expect(field).toHaveValue('/npc ')
  await field.fill('/npc the harbour ferryman')
  expect(calls).toEqual([])
  await page.getByRole('button', { name: 'Sage', exact: true }).click()
  await expect(page.getByPlaceholder('Ask…')).toHaveValue('')
  await page.getByRole('button', { name: 'GM', exact: true }).click()
  await expect(field).toHaveValue('/npc the harbour ferryman')
  await page.getByRole('button', { name: 'Run NPC tool' }).click()
  const result = page.locator('.assistant-lane[data-state="done"]')
  await expect(result.getByText(DOCUMENT_TITLE)).toBeVisible()
  expect(calls).toHaveLength(1)
  await page.reload()
  await expect(result.getByText(DOCUMENT_TITLE)).toBeVisible()
  await expect(result).toHaveCount(1)
  await expect(page.getByRole('heading', { name: DOCUMENT_TITLE })).toBeHidden()
  await result.getByRole('button', { name: 'Open in canvas' }).click()
  await expect(page.getByRole('heading', { name: DOCUMENT_TITLE })).toBeVisible()
  expect(calls).toHaveLength(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('a hydrated monster card saves without replacing the canvas and opens on a second press', async ({ page }) => {
  await prepWorkbench(page)
  const invocation = toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() })
  const entryId = 'ent_e2eWorkbenchMonster000001'
  const statBlockId = 'doc_e2ePromotedStatBlock000001'
  const called: string[] = []
  const saved = {
    schema_version: 1, document_id: statBlockId, campaign_id: CAMPAIGN_ID, type: 'statblock', type_version: 1,
    data: { name: 'Drowned Thing', ac: 14, hp: 42 }, write_revision: 1, archived: false,
    created_at: invocation.created_at, updated_at: invocation.updated_at, version: {
      number: 1, author: 'assistant', summary: '', created_at: invocation.created_at,
      sealed: false, changed_fields: ['name', 'ac', 'hp'], restored_from: null,
    },
  }
  const savePath = `/campaigns/${CAMPAIGN_ID}/conversations/${THREAD}/timeline/${entryId}/promote`
  await page.route((url) => url.pathname === `/conversations/${THREAD}/timeline`, (route) => route.fulfill({ json: {
    schema_version: 1, conversation_id: THREAD, next_cursor: null,
    items: [{
      schema_version: 1, entry_id: entryId, entry_kind: 'tool',
      created_at: invocation.created_at, brief: 'A guard for the shrine', invocation,
    }],
  } }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/tool-availability`,
    (route) => route.fulfill({ json: Object.fromEntries(TOOL_IDS.map((id) => [id, null])) }))
  await page.route((url) => url.pathname === savePath, (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({})
    called.push(savePath)
    return route.fulfill({ json: saved })
  })
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/documents/${statBlockId}`,
    (route) => route.fulfill({ json: saved }))
  await page.goto(WORKSPACE)
  const save = page.getByRole('button', { name: 'Save to Bestiary' })
  await expect(save).toBeVisible()
  expect(called).toEqual([])
  await save.click()
  const open = page.getByRole('button', { name: 'Open in Bestiary' })
  await expect(open).toBeFocused()
  await expect(page).not.toHaveURL(/document=/)
  await open.click()
  await expect(page.getByRole('heading', { name: 'Drowned Thing', level: 2 })).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`document=${statBlockId}`))
  expect(called).toEqual([savePath])
})
