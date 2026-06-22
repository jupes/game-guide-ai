import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SourceList } from './SourceList'
import { ExchangeView } from './ExchangeView'
import { ChatForm } from './ChatForm'
import App from '../App'
import { ThemeProvider } from '../ds/theme'
import { AppNavProvider } from '../shell/AppNav'
import { CurrentUserProvider } from '../shell/currentUser'
import type { ChatResponse, Source } from '../api'
import type { Exchange } from '../useChat'

const SOURCES: Source[] = [
  { book: 'mm-5e', chapter: 'Bestiary', section: 'Stat Block', entity: 'Basilisk', page: 12, snippet: 'Armor Class 15 …' },
  { book: 'vgm-5e', chapter: null, section: null, entity: 'Basilisk Lore', page: 80, snippet: 'Travelers find statues …' },
]

const GROUNDED: ChatResponse = { answer: 'A basilisk petrifies with its gaze [1].', sources: SOURCES, answerable: true }
const REFUSAL: ChatResponse = { answer: "I couldn't find that in the D&D 5e sources I have.", sources: [], answerable: false }

const ex = (over: Partial<Exchange>): Exchange => ({ id: 1, prompt: 'What is a Basilisk?', status: 'done', ...over })

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

describe('ExchangeView', () => {
  it('shows a loading state while pending', () => {
    render(<ExchangeView exchange={ex({ status: 'pending' })} onRetry={() => {}} />)
    expect(screen.getByText(/consulting the tomes/i)).toBeInTheDocument()
  })

  it('renders the answer and sources when done', () => {
    render(<ExchangeView exchange={ex({ response: GROUNDED })} onRetry={() => {}} />)
    expect(screen.getByText(/petrifies with its gaze/)).toBeInTheDocument()
    expect(screen.getByText(/2 sources/i)).toBeInTheDocument()
  })

  it('styles refusals distinctly and shows no sources block', () => {
    render(<ExchangeView exchange={ex({ response: REFUSAL })} onRetry={() => {}} />)
    const answer = screen.getByText(/couldn't find that/i)
    expect(answer.closest('.answer')).toHaveClass('refusal')
    // the count badge ("N sources") must be absent — note the refusal answer
    // text itself contains the word "sources", so match the badge shape
    expect(screen.queryByText(/\d+ sources?/i)).not.toBeInTheDocument()
  })

  it('renders an error with a retry button that re-sends the prompt', async () => {
    const onRetry = vi.fn()
    render(<ExchangeView exchange={ex({ status: 'error', error: 'Service unavailable' })} onRetry={onRetry} />)
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(onRetry).toHaveBeenCalledWith('What is a Basilisk?')
  })
})

describe('ChatForm', () => {
  it('submits the prompt and clears, Enter submits', async () => {
    const onSend = vi.fn()
    render(<ChatForm onSend={onSend} disabled={false} />)
    const box = screen.getByRole('textbox')
    await userEvent.type(box, 'What is a Basilisk?{Enter}')
    expect(onSend).toHaveBeenCalledWith('What is a Basilisk?')
    expect(box).toHaveValue('')
  })

  it('disables the form while a request is pending', () => {
    render(<ChatForm onSend={() => {}} disabled={true} />)
    expect(screen.getByRole('textbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: /ask/i })).toBeDisabled()
  })

  it('does not submit empty input', async () => {
    const onSend = vi.fn()
    render(<ChatForm onSend={onSend} disabled={false} />)
    await userEvent.type(screen.getByRole('textbox'), '{Enter}')
    expect(onSend).not.toHaveBeenCalled()
  })
})

describe('App (integration — shell renders Landing)', () => {
  it('renders the Landing screen by default', () => {
    render(
      <ThemeProvider>
        <AppNavProvider>
          <CurrentUserProvider>
            <App />
          </CurrentUserProvider>
        </AppNavProvider>
      </ThemeProvider>,
    )
    expect(screen.getByText('Enter the Tavern')).toBeInTheDocument()
    expect(screen.getByText('Aetheril')).toBeInTheDocument()
  })
})
