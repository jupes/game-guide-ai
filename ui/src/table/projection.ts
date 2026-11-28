import type { TableProjection, TableSlot } from '../gm/contracts'
import { documentTypeById, labelFor } from '../gm/registry'

type Value = TableProjection['fields'][number]['value']

function fieldText(value: Value): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.map((item) => typeof item === 'string' ? item : `${item.name}\n${item.text}`).join('\n')
  // Media handles belong to the table media route. They are never labels or URLs here.
  if ('handle' in value) return ''
  return Object.entries(value).map(([ability, score]) => `${ability.toUpperCase()} ${score}`).join('\n')
}

function section(document: Document, label: string, projection: TableProjection): HTMLElement | null {
  const type = documentTypeById(projection.type)
  if (type === undefined) return null
  const article = document.createElement('article')
  const heading = document.createElement('h2')
  heading.textContent = label
  article.append(heading)
  for (const { key, value } of projection.fields) {
    const text = fieldText(value)
    if (text === '') continue
    if (key === 'name') {
      const name = document.createElement('h3')
      name.textContent = text
      article.append(name)
      continue
    }
    const title = labelFor(type, key)
    if (title === undefined) continue
    const field = document.createElement('section')
    const fieldHeading = document.createElement('h3')
    fieldHeading.textContent = title
    const body = document.createElement('p')
    body.textContent = text
    field.append(fieldHeading, body)
    article.append(field)
  }
  return article.childElementCount > 1 ? article : null
}

export function tableProjections(target: HTMLElement) {
  type DisplayedSlot = { key: string; seq: number; article: HTMLElement | null }
  const previous = new Map<TableSlot['slot'], DisplayedSlot>()

  function update(slots: readonly TableSlot[]): 'shared' | 'stopped' | 'unchanged' {
    let shared = false
    let stopped = false
    for (const [slot, label] of [['mine', 'For you'], ['table', 'Shared with the table']] as const) {
      const frame = slots.find(entry => entry.slot === slot)
      const content = frame?.content ?? null
      const key = JSON.stringify(content)
      const seq = frame?.seq ?? 0
      const prior = previous.get(slot)
      if (prior?.key === key) {
        if (prior.article && seq > prior.seq) shared = true
        prior.seq = seq
        continue
      }
      const article = content && section(target.ownerDocument, label, content)
      if (prior?.article && article?.isEqualNode(prior.article)) {
        previous.set(slot, { key, seq, article: prior.article })
        if (seq > prior.seq) shared = true
        continue
      }
      previous.set(slot, { key, seq, article })
      if (article) {
        if (prior?.article) prior.article.replaceWith(article)
        else if (slot === 'mine') target.prepend(article)
        else target.append(article)
        shared = true
      } else if (prior?.article) {
        prior.article.remove()
        stopped = true
      }
    }
    return shared ? 'shared' : stopped ? 'stopped' : 'unchanged'
  }

  function clear(): void {
    previous.clear()
    target.replaceChildren()
  }

  return { update, clear }
}
