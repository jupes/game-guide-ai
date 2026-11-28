import { afterEach, expect, it, vi } from 'vitest'
import { clearSignedOutStorage } from './signOutStorage'

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
})

it('clears app namespaces and both legacy keys, while preserving unrelated local and session data', () => {
  const owned = ['game-guide-ai:profile', 'game-guide-ai:profile:old', 'game-guide-ai:conversations:guest',
    'game-guide-ai:gm-pins:owner', 'game-guide-ai:gm-pending-stops:owner', 'game-guide-ai:gm-preview-volume',
    'rag-chat:conversations', 'aetheril-theme']
  const other = ['game-guide-ai-backup:mine', 'rag-chat:other', 'aetheril-unrelated']
  for (const area of [localStorage, sessionStorage]) for (const key of [...owned, ...other]) area.setItem(key, 'private')
  clearSignedOutStorage()
  for (const area of [localStorage, sessionStorage]) {
    for (const key of owned) expect(area.getItem(key)).toBeNull()
    for (const key of other) expect(area.getItem(key)).toBe('private')
  }
})

it('a blocked storage area does not prevent clearing the remaining area', () => {
  sessionStorage.setItem('game-guide-ai:profile', 'private')
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new DOMException('Blocked', 'SecurityError') })
  expect(clearSignedOutStorage).not.toThrow()
  expect(sessionStorage.getItem('game-guide-ai:profile')).toBeNull()
})
