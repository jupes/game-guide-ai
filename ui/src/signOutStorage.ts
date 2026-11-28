/** Fallback for browsers that ignore Clear-Site-Data. Read only key names:
 * conversation titles, profiles and outstanding GM commands can be private. */
export function clearSignedOutStorage(): void {
  for (const name of ['localStorage', 'sessionStorage'] as const) {
    try {
      const storage = window[name]
      for (let i = storage.length - 1; i >= 0; i--) {
        const key = storage.key(i)
        if (key?.startsWith('game-guide-ai:') || key === 'rag-chat:conversations' || key === 'aetheril-theme') {
          storage.removeItem(key)
        }
      }
    } catch {
      // Storage may be disabled. Still clear the other area and finish sign-out.
    }
  }
}
