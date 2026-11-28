import { expect, it } from 'vitest'
import { streamTable, tablePicture, type TablePicture } from './stream'

const event = { schema_version: 1 }
const session = { ...event, event: 'session', role: 'guest', audio: true }
const snapshot = { ...event, event: 'snapshot', slots: [{ slot: 'table', seq: 0, content: null }] }
const ready = { ...event, event: 'ready' }
const frame = (slot: 'one_shot' | 'ambience', seq: number) => ({ ...event, event: 'audio', slot, seq, playing: null })

it('keeps per-slot audio sequence marks and resnapshots without applying a gap', async () => {
  const frames = [session, snapshot, frame('ambience', 3), ready, frame('ambience', 2),
    frame('one_shot', 1), frame('ambience', 4), frame('one_shot', 3)]
  const body = frames.map(f => `event: ${f.event}\ndata: ${JSON.stringify(f)}\n\n`).join('')
  const received: TablePicture[] = []
  const end = await streamTable('cmp_A', new AbortController().signal, async () => new Response(body, {
    headers: { 'Content-Type': 'text/event-stream', 'X-Table-Audio-Context': 'a'.repeat(64) },
  }), { opened() {}, bytes() {}, picture: value => received.push(value) })
  expect(end.kind).toBe('reconnect')
  expect(received.map(p => p.audio.map(f => [f.slot, f.seq]))).toEqual([
    [['ambience', 3]], [['ambience', 3], ['one_shot', 1]], [['one_shot', 1], ['ambience', 4]],
  ])
  expect(received[0].audioContext).toBe('a'.repeat(64))
})

it('rejects an ambiguous opening audio slot', () => {
  expect(tablePicture({ ...event, frames: [session, snapshot, frame('ambience', 1), frame('ambience', 2), ready] }))
    .toEqual({ kind: 'retry' })
})

it('recognizes an explicit terminal before ready without publishing a partial picture', async () => {
  const frames = [session, snapshot, { ...event, event: 'reconnect' }]
  const body = frames.map(f => `event: ${f.event}\ndata: ${JSON.stringify(f)}\n\n`).join('')
  const received: TablePicture[] = []
  const end = await streamTable('cmp_A', new AbortController().signal, async () => new Response(body, {
    headers: { 'Content-Type': 'text/event-stream' },
  }), { opened() {}, bytes() {}, picture: value => received.push(value) })
  expect(end).toEqual({ kind: 'reconnect' })
  expect(received).toEqual([])
})

it.each([null, 'abridged'])('keeps the public snapshot readable with missing or malformed sound context (%s)', context => {
  const result = tablePicture({ ...event, frames: [session, snapshot, ready] }, 0, context)
  expect(result).toMatchObject({ kind: 'live', slots: snapshot.slots, audioContext: null })
})
