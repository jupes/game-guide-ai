import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Cue } from '../gm/contracts'
import type { MixerSlots } from '../table/audioMixer'
import { readCue } from '../gm/cueApi'
import type { AudioFrame } from './cueStream'
import { GmCueMonitor } from './gmCueMonitor'

const mixer = vi.hoisted(() => ({
  accepted: true, apply: vi.fn<(slots: MixerSlots, options?: { resync?: boolean }) => void>(),
  clear: vi.fn(), silence: vi.fn(), setVolume: vi.fn(), setMuted: vi.fn(), dispose: vi.fn(), unlock: vi.fn(),
}))
vi.mock('../table/audioMixer', () => ({
  emptyAudioSlots: { ambience: null, one_shot: null }, createAudioMixer: () => mixer,
}))
vi.mock('../gm/cueApi', () => ({ readCue: vi.fn() }))

const campaign = 'cmp_harbor', started = '2026-01-01T12:00:00Z'
function frame(slot: AudioFrame['slot'], id: string | null, seq = 1): AudioFrame {
  return { schema_version: 1, event: 'audio', session_id: 'ses_harbor', gen: 1, audio_epoch: seq,
    slot, seq, playing: id ? { cue_id: id, title: 'Harbor', started_at: started,
      start_offset_ms: 0, duration_ms: 12_000, loop: slot === 'ambience' } : null }
}
function cue(id: string): Cue {
  return { schema_version: 1, campaign_id: campaign, cue_id: id, asset_id: id + '_asset', title: 'Harbor',
    kind: 'ambience', duration_ms: 12_000, archived: false, created_at: started, updated_at: started }
}
function pendingCue() {
  let finish!: (value: Cue) => void
  let fail!: () => void
  const pending = new Promise<Cue>((resolve, reject) => { finish = resolve; fail = () => reject(new Error()) })
  return { pending, finish, fail }
}
const applied = () => mixer.apply.mock.lastCall?.[0]
let monitor: GmCueMonitor
beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  vi.mocked(readCue).mockReset()
  monitor = new GmCueMonitor(campaign, vi.fn())
  monitor.start()
})
afterEach(() => { monitor.dispose(); vi.useRealTimers() })

describe('GM monitoring of the current session', () => {
  it('starts a successfully resolved slot even when the other lookup fails first', async () => {
    const a = pendingCue(), b = pendingCue()
    vi.mocked(readCue).mockReturnValueOnce(a.pending).mockReturnValueOnce(b.pending)
    monitor.update([frame('ambience', 'rain'), frame('one_shot', 'bell')], true, true, 0)
    a.fail()
    await vi.advanceTimersByTimeAsync(0)
    b.finish(cue('bell'))
    await vi.advanceTimersByTimeAsync(0)
    expect(applied()?.ambience).toBeNull()
    expect(applied()?.one_shot?.url).toBe('/campaigns/cmp_harbor/assets/bell_asset')
  })

  it('never revives expired playback when a later disconnected update or preview completion arrives', async () => {
    const live = [frame('ambience', 'rain'), frame('one_shot', null)]
    vi.mocked(readCue).mockResolvedValue(cue('rain'))
    monitor.update(live, true, true, 0)
    await vi.advanceTimersByTimeAsync(0)
    expect(applied()?.ambience?.url).toContain('rain_asset')
    const media = document.createElement('audio')
    monitor.previewStarted(media)
    monitor.update(live, false, true, 0)
    await vi.advanceTimersByTimeAsync(10_001)
    expect(mixer.silence).toHaveBeenCalledOnce()
    mixer.apply.mockClear()
    monitor.update(live, false, true, 0)
    monitor.previewEnded(media)
    expect(mixer.apply).not.toHaveBeenCalled()
    monitor.update(live, true, true, 0)
    await vi.advanceTimersByTimeAsync(0)
    expect(applied()?.ambience?.url).toContain('rain_asset')
    expect(mixer.apply.mock.lastCall?.[1]?.resync).toBe(true)
  })

  it('ignores late metadata after an explicit Stop or a scope being disposed', async () => {
    const a = pendingCue(), b = pendingCue()
    vi.mocked(readCue).mockReturnValueOnce(a.pending).mockReturnValueOnce(b.pending)
    monitor.update([frame('ambience', 'rain')], true, true, 0)
    monitor.update([frame('ambience', null, 2)], true, true, 0)
    expect(mixer.clear).toHaveBeenCalled()
    mixer.apply.mockClear()
    a.finish(cue('rain'))
    await vi.advanceTimersByTimeAsync(0)
    expect(mixer.apply).not.toHaveBeenCalled()
    monitor.update([frame('ambience', 'waves', 3)], true, true, 0)
    monitor.dispose()
    mixer.apply.mockClear()
    b.finish(cue('waves'))
    await vi.advanceTimersByTimeAsync(0)
    expect(mixer.apply).not.toHaveBeenCalled()
  })

  it('restores only the latest live cue after the active private preview ends', async () => {
    vi.mocked(readCue).mockImplementation(async (_campaign, id) => cue(id))
    monitor.update([frame('ambience', 'rain')], true, true, 0)
    await vi.advanceTimersByTimeAsync(0)
    const first = document.createElement('audio'), second = document.createElement('audio')
    monitor.previewStarted(first)
    monitor.previewStarted(second)
    mixer.apply.mockClear()
    monitor.update([frame('ambience', 'waves', 2)], true, true, 0)
    monitor.previewEnded(first)
    await vi.advanceTimersByTimeAsync(0)
    expect(mixer.apply).not.toHaveBeenCalled()
    monitor.previewEnded(second)
    expect(applied()?.ambience?.url).toContain('waves_asset')
    expect(mixer.apply.mock.lastCall?.[1]?.resync).toBe(true)
  })
})
