import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SourceList } from './SourceList'
import App from '../App'
import * as api from '../api'
import { ThemeProvider } from '../ds/theme'
import { AppNavProvider } from '../shell/AppNav'
import { CurrentUserProvider } from '../shell/currentUser'
import { ConversationStoreProvider } from '../shell/ConversationStoreContext'
import { MemoryConversationStore } from '../shell/conversationStore'
import type { Source } from '../api'

const SOURCES: Source[] = [
  { book: 'mm-5e', chapter: 'Bestiary', section: 'Stat Block', entity: 'Basilisk', page: 12, snippet: 'Armor Class 15 …' },
  { book: 'vgm-5e', chapter: null, section: null, entity: 'Basilisk Lore', page: 80, snippet: 'Travelers find statues …' },
]

describe('SourceList', () => {
  it('is collapsed by default with a count badge, expands on click', async () => {
    render(<SourceList sources={SOURCES} />)
    expect(screen.getByText(/2 sources/i)).toBeInTheDocument()
    expect(screen.queryByText(/Armor Class 15/)).not.toBeVisible()
    await userEvent.click(screen.getByText(/2 sources/i))
    expect(screen.getByText(/Armor Class 15/)).toBeVisible()
    expect(screen.getByText(/mm-5e/)).toBeInTheDocument()
    expect(screen.getByText(/p\.12/)).toBeInTheDocument()
  })
})

describe('App (integration — shell renders Landing)', () => {
  it('renders the Landing screen by default', async () => {
    // App holds a loading state until the session check resolves (x5bz.2), so
    // stub it as an authenticated tester and wait for the shell.
    vi.spyOn(api, 'getMe').mockResolvedValue({
      kind: 'ok', user: { email: 'tester@example.com', role: 'player' },
    })
    render(
      <ThemeProvider>
        <AppNavProvider>
          <CurrentUserProvider>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <App />
            </ConversationStoreProvider>
          </CurrentUserProvider>
        </AppNavProvider>
      </ThemeProvider>,
    )
    expect(await screen.findByText('Enter the Tavern')).toBeInTheDocument()
    expect(screen.getByText('Aetheril')).toBeInTheDocument()
  })
})
