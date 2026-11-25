import type { CanvasDirtySource, FlushOutcome } from '../shell/lossGuard'
import type { CanvasSaveStatus } from './canvasStatus'
import type { Document, FieldValue } from './contracts'
import { getDocument, saveDocumentField } from './documentApi'
import type { FieldStatus } from './documentFields'

const IDLE_MS = 1500

type Draft = {
  value: FieldValue | undefined
  base: number
  due: number
  failure?: { message?: string; retryable: boolean }
  conflictRevision?: number
}

interface SaveSnapshot {
  fields: Readonly<Record<string, FieldStatus>>
  status: CanvasSaveStatus
  retryable: boolean
  reset: number
}

function valueOf(document: Document, key: string): FieldValue | undefined {
  return Object.hasOwn(document.data, key) ? document.data[key] : undefined
}

function sameValue(a: FieldValue | undefined, b: FieldValue | undefined): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function isOffline(): boolean {
  return navigator.onLine === false
}

/** One queue per open document. Each draft keeps the revision it was first typed against. */
export class DocumentAutosave implements CanvasDirtySource {
  private readonly drafts = new Map<string, Draft>()
  private readonly listeners = new Set<() => void>()
  private snapshot: SaveSnapshot = { fields: {}, status: 'saved', retryable: false, reset: 0 }
  private timer: ReturnType<typeof setTimeout> | undefined
  private inFlight: { key: string; draft: Draft; done: Promise<void> } | null = null
  private active = false
  private document: Document
  private readonly accept: (document: Document) => void
  private readonly fetchImpl?: typeof fetch

  constructor(
    document: Document,
    accept: (document: Document) => void,
    fetchImpl?: typeof fetch,
  ) {
    this.document = document
    this.accept = accept
    this.fetchImpl = fetchImpl
  }

  getSnapshot = (): SaveSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  mount(): () => void {
    this.active = true
    return () => {
      this.active = false
      clearTimeout(this.timer)
    }
  }

  sync(document: Document): void {
    if (document.write_revision < this.document.write_revision) return
    this.document = document
    this.publish()
  }

  private adopt(document: Document): void {
    if (document.write_revision < this.document.write_revision) return
    this.document = document
    this.accept(document)
  }

  private publish(): void {
    const fields: Record<string, FieldStatus> = Object.create(null)
    let status: CanvasSaveStatus = this.inFlight !== null ? 'saving' : 'saved'
    let retryable = false
    for (const [key, draft] of this.drafts) {
      if (draft.conflictRevision !== undefined) {
        fields[key] = { state: 'conflict', latest: valueOf(this.document, key), mine: draft.value }
        status = 'conflict'
      } else if (draft.failure !== undefined) {
        fields[key] = { state: 'error', ...draft.failure }
        retryable ||= draft.failure.retryable
        if (status !== 'conflict') status = 'error'
      } else if (this.inFlight?.key === key && this.inFlight.draft === draft) {
        fields[key] = { state: 'saving' }
      } else {
        fields[key] = { state: 'editing' }
        if (status === 'saved') status = 'unsaved'
      }
    }
    this.snapshot = { ...this.snapshot, fields, status, retryable }
    for (const listener of this.listeners) listener()
  }

  private stage(key: string, value: FieldValue | undefined, immediate: boolean): void {
    if (!this.active || this.document.archived) return
    const prior = this.drafts.get(key)
    if (prior?.conflictRevision !== undefined) {
      this.drafts.set(key, { ...prior, value })
      this.publish()
      return
    }
    // Blur following an idle save must not enqueue the very same save again.
    if (prior !== undefined && sameValue(value, prior.value) &&
        (this.inFlight?.draft === prior || prior.failure !== undefined)) return
    if (immediate && value === undefined) return
    if (value !== undefined && sameValue(value, valueOf(this.document, key)) && this.inFlight?.key !== key) {
      if (this.drafts.delete(key)) this.advance()
      return
    }
    this.drafts.set(key, {
      value,
      base: prior?.base ?? this.document.write_revision,
      due: immediate ? 0 : Date.now() + IDLE_MS,
    })
    this.advance()
  }

  draft = (key: string, value: FieldValue): void => this.stage(key, value, false)
  invalidDraft = (key: string): void => this.stage(key, undefined, false)
  commit = (key: string, value: FieldValue): void => this.stage(key, value, true)

  cancel = (key: string): void => {
    // A write that has already been sent remains tracked until the reply.
    const sent = this.inFlight
    if (sent?.key === key) this.drafts.set(key, sent.draft)
    else this.drafts.delete(key)
    this.advance()
  }

  keepMine = (key: string): void => {
    const draft = this.drafts.get(key)
    if (draft?.conflictRevision === undefined || draft.value === undefined) return
    this.drafts.set(key, { value: draft.value, base: this.document.write_revision, due: 0 })
    this.advance()
  }

  retry = (key?: string): void => {
    for (const [field, draft] of this.drafts) {
      if ((key === undefined || field === key) && draft.failure?.retryable === true) {
        this.drafts.set(field, { value: draft.value, base: draft.base, due: 0 })
      }
    }
    this.advance()
  }

  isDirty = (): boolean => this.drafts.size > 0 || this.inFlight !== null

  async flush(): Promise<FlushOutcome> {
    if (!this.active || !this.isDirty()) return 'saved'
    if (isOffline()) return 'offline'
    for (const [key, draft] of this.drafts) {
      if (draft.conflictRevision === undefined && draft.failure?.retryable !== false && this.inFlight?.draft !== draft) {
        this.drafts.set(key, { value: draft.value, base: draft.base, due: 0 })
      }
    }
    this.advance()
    while (this.inFlight !== null) await this.inFlight.done
    if (!this.isDirty()) return 'saved'
    if (isOffline()) return 'offline'
    return [...this.drafts.values()].every((draft) => draft.failure?.retryable === true) ? 'retryable' : 'failed'
  }

  discard = (): void => {
    this.drafts.clear()
    this.snapshot = { ...this.snapshot, reset: this.snapshot.reset + 1 }
    this.advance()
  }

  private advance(): void {
    clearTimeout(this.timer)
    this.publish()
    if (!this.active || this.inFlight !== null) return
    let next = Infinity
    for (const [key, draft] of this.drafts) {
      if (draft.value === undefined || draft.failure !== undefined || draft.conflictRevision !== undefined) continue
      if (draft.due > Date.now()) {
        next = Math.min(next, draft.due)
        continue
      }
      const done = this.write(key, draft, draft.value)
      this.inFlight = { key, draft, done }
      this.publish()
      return
    }
    if (next !== Infinity) this.timer = setTimeout(() => this.advance(), Math.max(0, next - Date.now()))
  }

  private async write(key: string, sent: Draft, value: FieldValue): Promise<void> {
    const document = this.document
    const answer = await saveDocumentField(document, key, value, sent.base, this.fetchImpl)
    if (!this.active) {
      this.inFlight = null
      return
    }
    if (answer.kind === 'ok') {
      this.adopt(answer.document)
      const current = this.drafts.get(key)
      if (current === sent) this.drafts.delete(key)
      else if (current !== undefined) {
        // Only the draft for this field may advance through its own acknowledged write.
        this.drafts.set(key, { ...current, base: answer.document.write_revision })
      }
    } else if (answer.kind === 'conflict') {
      const latest = await getDocument(document.campaign_id, document.document_id, this.fetchImpl)
      const current = this.drafts.get(key)
      if (this.active && current !== undefined) {
        if (latest.kind === 'ok' && latest.document.type === document.type &&
            latest.document.write_revision >= answer.conflict.write_revision) {
          this.adopt(latest.document)
          this.drafts.set(key, { ...current, conflictRevision: latest.document.write_revision })
        } else {
          this.drafts.set(key, { ...current, failure: { retryable: true, message: "Couldn't read the latest value" } })
        }
      }
    } else if (this.drafts.get(key) === sent) {
      this.drafts.set(key, { ...sent, failure: { message: answer.message, retryable: answer.retryable } })
    }
    this.inFlight = null
    this.advance()
  }
}
