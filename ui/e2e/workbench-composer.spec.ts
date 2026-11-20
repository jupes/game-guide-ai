import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'
import { CAMPAIGN_ID, DOCUMENT, DOCUMENT_ID, DOCUMENT_TITLE, HISTORY, installWorkbenchRoutes } from './workbenchStubs'
import { DocumentHistoryPageSchema, DocumentVersionSchema, TOOL_IDS, type FieldPatchRequest, type ToolInvocationRequest } from '../src/gm/contracts'
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

test('phone autosave keeps new typing and resolves two-tab conflicts before switching channels', async ({ page }) => {
  await prepWorkbench(page)
  const requests: FieldPatchRequest[] = []
  let server = DOCUMENT
  let firstWrite: () => void = () => {}
  const firstWriteReady = new Promise<void>((resolve) => { firstWrite = resolve })
  await page.route((url) => url.pathname === `/conversations/${THREAD}/timeline`, (route) => route.fulfill({ json: {
    schema_version: 1, conversation_id: THREAD, next_cursor: null, items: [],
  } }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/tool-availability`,
    (route) => route.fulfill({ json: Object.fromEntries(TOOL_IDS.map((id) => [id, null])) }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/documents/${DOCUMENT_ID}`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: server })
    expect(route.request().method()).toBe('PATCH')
    const patch = route.request().postDataJSON() as FieldPatchRequest
    requests.push(patch)
    if (requests.length === 1) await firstWriteReady
    if (patch.base_write_revision < server.write_revision) {
      return route.fulfill({ status: 409, json: { detail: {
        code: 'conflict', message: 'This document changed elsewhere', retryable: false,
        conflict: { write_revision: server.write_revision, fields: ['voice'] },
      } } })
    }
    server = { ...server, write_revision: server.write_revision + 1, data: { ...server.data, ...patch.fields } }
    return route.fulfill({ json: server })
  })
  await page.goto(`${WORKSPACE}&document=${DOCUMENT_ID}`)
  const canvas = page.getByRole('region', { name: DOCUMENT_TITLE })
  const edit = canvas.getByRole('button', { name: 'Edit Voice' })
  const input = canvas.getByRole('textbox', { name: 'Voice', exact: true })
  await edit.click()
  await input.fill('Low and quick')
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]).toMatchObject({ base_write_revision: 4, fields: { voice: 'Low and quick' } })
  await input.fill('Low and steady')
  await input.press('Control+s')
  expect(requests).toHaveLength(1)
  firstWrite()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1]).toMatchObject({ base_write_revision: 5, fields: { voice: 'Low and steady' } })
  await expect(input).toHaveValue('Low and steady')
  await expect(canvas.getByText('Saved', { exact: true })).toBeVisible()

  server = { ...server, write_revision: 7, data: { ...server.data, voice: 'Clear and even' } }
  await input.fill('A gravelly whisper')
  await input.press('Control+s')
  const conflict = canvas.getByRole('group', { name: 'Voice changed elsewhere' })
  await expect(conflict.getByText('A gravelly whisper')).toBeVisible()
  await expect(conflict.getByText('Clear and even')).toBeVisible()
  await page.getByRole('button', { name: 'Sage', exact: true }).click()
  const guard = page.getByRole('dialog', { name: /unsaved changes/i })
  await expect(guard.getByRole('button', { name: 'Keep editing' })).toBeFocused()
  await guard.getByRole('button', { name: 'Keep editing' }).click()
  await expect(input).toHaveValue('A gravelly whisper')
  await conflict.getByRole('button', { name: 'Keep mine' }).click()
  await expect(conflict).toBeHidden()
  expect(requests[3]).toMatchObject({ base_write_revision: 7, fields: { voice: 'A gravelly whisper' } })
  await expect.poll(() => server.data.voice).toBe('A gravelly whisper')
  await expect(requests).toHaveLength(4)

  server = { ...server, write_revision: 9, data: { ...server.data, voice: 'The latest voice' } }
  await expect(edit).toBeVisible()
  await edit.click()
  await input.fill('A draft to abandon')
  await input.press('Control+s')
  await conflict.getByRole('button', { name: 'Use latest' }).click()
  await expect(canvas.getByText('The latest voice', { exact: true })).toBeVisible()
  expect(requests).toHaveLength(5)

  await page.getByRole('button', { name: 'Sage', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Sage', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'Spell', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Spell', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'GM', exact: true }).click()
  await expect(canvas.getByText('The latest voice', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('history restore on a phone flushes drafts and appends a new version', async ({ page }) => {
  await prepWorkbench(page)
  const documentPath = `/campaigns/${CAMPAIGN_ID}/documents/${DOCUMENT_ID}`
  const writes: string[] = []
  let server = { ...DOCUMENT, version: DocumentVersionSchema.parse(DOCUMENT.version) }
  let history = DocumentHistoryPageSchema.parse(HISTORY)
  let patchesFail = true
  await page.route((url) => url.pathname === `/conversations/${THREAD}/timeline`, (route) => route.fulfill({ json: {
    schema_version: 1, conversation_id: THREAD, next_cursor: null, items: [],
  } }))
  await page.route((url) => url.pathname === `/campaigns/${CAMPAIGN_ID}/tool-availability`,
    (route) => route.fulfill({ json: Object.fromEntries(TOOL_IDS.map((id) => [id, null])) }))
  await page.route((url) => url.pathname === documentPath, (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: server })
    const patch = route.request().postDataJSON() as FieldPatchRequest
    expect(patch).toMatchObject({ base_write_revision: 4, fields: { voice: 'Voice to preserve in history' } })
    writes.push('patch')
    if (patchesFail) return route.fulfill({ status: 503, json: {} })
    server = { ...server, write_revision: 5, data: { ...server.data, ...patch.fields } }
    return route.fulfill({ json: server })
  })
  await page.route((url) => url.pathname === `${documentPath}/versions`, (route) => route.fulfill({ json: history }))
  await page.route((url) => url.pathname === `${documentPath}/restore`, (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ schema_version: 1, version_number: 1 })
    writes.push('restore')
    if (writes.filter((write) => write === 'restore').length === 1) return route.fulfill({ status: 503, json: {} })
    const version = { ...server.version, number: 3, restored_from: 1 }
    server = { ...server, write_revision: 6, version, data: { ...server.data, voice: 'The first voice' } }
    history = { ...history, items: [version, ...history.items] }
    return route.fulfill({ json: server })
  })

  await page.goto(`${WORKSPACE}&document=${DOCUMENT_ID}`)
  const canvas = page.getByRole('region', { name: DOCUMENT_TITLE })
  await canvas.getByRole('button', { name: 'More canvas actions' }).click()
  await canvas.getByRole('menuitem', { name: 'Version history' }).click()
  const versions = canvas.getByRole('region', { name: 'Version history' })
  const restore = versions.getByRole('button', { name: 'Restore v1', exact: true })
  await expect(restore).toBeVisible()
  await canvas.getByRole('button', { name: 'Edit Voice' }).click()
  const voice = canvas.getByRole('textbox', { name: 'Voice', exact: true })
  await voice.fill('Voice to preserve in history')
  await restore.click()
  const guard = page.getByRole('dialog', { name: /unsaved changes/i })
  await expect(guard).toBeVisible()
  expect(writes).toContain('patch')
  expect(writes).not.toContain('restore')
  patchesFail = false
  await guard.getByRole('button', { name: 'Try saving again' }).click()
  await expect(versions.getByRole('alert')).toHaveText("Couldn't restore v1. Select Restore v1 to try again.")
  expect(server.data.voice).toBe('Voice to preserve in history')
  expect(writes.slice(-2)).toEqual(['patch', 'restore'])

  await restore.click()
  await expect(versions.getByText('Restored from v1')).toBeVisible()
  await expect(versions.getByRole('button', { name: 'Restore v2' })).toBeVisible()
  await expect(canvas.getByText('The first voice', { exact: true })).toBeVisible()
  await expect(canvas.getByRole('heading', { name: DOCUMENT_TITLE })).toBeFocused()
  await expect(versions.getByRole('alert')).toBeHidden()
})
