import { createAudioMixer, emptyAudioSlots, type AudioPresence, type MixerSlots, type MixerState } from './audioMixer'
import './audioControls.css'

const initialState: MixerState = {
  accepted: false, muted: false, status: 'idle', needsGesture: false, presence: 'pending', playing: [],
}

/** The table entry is standalone vanilla DOM. Keep the same wording as the
 * design's AudioConsentPrompt, with no access to a cue title or filename. */
export function mountTableAudio(
  root: HTMLElement, onPresence: (state: AudioPresence, playing: readonly string[]) => void, now?: () => number,
) {
  const doc = root.ownerDocument
  const section = doc.createElement('section')
  section.className = 'table-sound'
  section.setAttribute('aria-label', 'Table sound')
  const accept = doc.createElement('button')
  accept.type = 'button'
  accept.className = 'table-action table-sound-enable'
  const label = doc.createElement('span')
  label.textContent = 'Tap to enable sound'
  const detail = doc.createElement('span')
  detail.className = 'table-sound-detail'
  detail.textContent = 'Your GM is playing something'
  accept.append(label, detail)
  const mute = doc.createElement('button')
  mute.type = 'button'
  mute.className = 'table-action'
  mute.textContent = 'Mute'
  const status = doc.createElement('p')
  status.setAttribute('role', 'status')
  section.append(accept, mute, status)
  root.replaceChildren(section)

  let enabled = false
  let waiting = false
  let lastPresence: AudioPresence | undefined
  let lastPlaying: readonly string[] = []
  let state = initialState
  function render(next = state): void {
    state = next
    section.hidden = !enabled
    const moveFocus = doc.activeElement === accept
    accept.hidden = next.accepted && !next.needsGesture && next.status !== 'error'
    label.textContent = next.accepted && !next.needsGesture ? 'Try sound again' : 'Tap to enable sound'
    detail.hidden = !waiting || next.accepted
    section.dataset.variant = next.accepted ? 'accepted' : waiting ? 'prompt' : 'banner'
    mute.hidden = !next.accepted && next.status !== 'error'
    mute.setAttribute('aria-pressed', String(next.muted))
    if (moveFocus && enabled && accept.hidden) mute.focus()
    const message = next.status === 'error' ? "Your browser can't play this sound"
      : next.status === 'loading' ? 'Loading sound…' : ''
    if (message !== status.textContent) status.textContent = message
    const presence = enabled ? next.presence : 'absent'
    const playing = presence === 'listening' ? next.playing : []
    if (presence !== lastPresence || playing.length !== lastPlaying.length || playing.some((url, i) => url !== lastPlaying[i])) {
      lastPresence = presence
      lastPlaying = playing
      onPresence(presence, playing)
    }
  }
  let mixer = createAudioMixer(doc, render, now)
  accept.onclick = () => { if (enabled) void mixer.unlock() }
  mute.onclick = () => { if (enabled) mixer.setMuted(!state.muted) }
  section.hidden = true
  return {
    get accepted() { return mixer.accepted },
    show(on: boolean, slots: MixerSlots, resync = false) {
      enabled = on
      waiting = Boolean(slots.ambience || slots.one_shot)
      mixer.apply(on ? slots : emptyAudioSlots, { resync })
    },
    silence() { mixer.silence() },
    hide() {
      enabled = false
      waiting = false
      mixer.clear()
    },
    reset() {
      enabled = false
      waiting = false
      mixer.dispose()
      mixer = createAudioMixer(doc, render, now)
      render(initialState)
    },
    dispose() {
      enabled = false
      mixer.dispose()
      render(initialState)
      section.remove()
    },
  }
}
