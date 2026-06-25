import { describe, it, expect, vi } from 'vitest'
import { renderHook, act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavProvider, AppNavContext, useAppNav } from './AppNav'
import type { AppNavState } from './AppNav'
import { Landing } from './Landing'

// ── CP-F3.1 — AppNav context behaviors (#12) ──────────────────────────────────

describe('AppNav context (#12)', () => {
  it('starts on the landing screen', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    expect(result.current.screen).toBe('landing')
  })

  it('enterWorkspace() sets screen to workspace with default mode sage', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace()
    })
    expect(result.current.screen).toBe('workspace')
    expect(result.current.mode).toBe('sage')
  })

  it('enterWorkspace("spell") sets screen to workspace with mode spell', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace('spell')
    })
    expect(result.current.screen).toBe('workspace')
    expect(result.current.mode).toBe('spell')
  })

  it('setMode("spell") updates mode to spell', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.setMode('spell')
    })
    expect(result.current.mode).toBe('spell')
  })

  it('backToLanding() sets screen to landing', () => {
    const { result } = renderHook(() => useAppNav(), { wrapper: AppNavProvider })
    act(() => {
      result.current.enterWorkspace()
    })
    act(() => {
      result.current.backToLanding()
    })
    expect(result.current.screen).toBe('landing')
  })
})

// ── CP-F3.2 — Landing component ───────────────────────────────────────────────

describe('Landing component', () => {
  it('renders brand text and CTA button', () => {
    const enterWorkspace = vi.fn()
    const mockState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace,
      setMode: vi.fn(),
      setConversationId: vi.fn(),
      backToLanding: vi.fn(),
    }

    render(
      <AppNavContext.Provider value={mockState}>
        <Landing />
      </AppNavContext.Provider>,
    )

    expect(screen.getByText('Aetheril')).toBeInTheDocument()
    expect(screen.getByText(/Grounded answers from the rulebooks/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Enter the Tavern/i })).toBeInTheDocument()
  })

  it('clicking CTA calls enterWorkspace', async () => {
    const enterWorkspace = vi.fn()
    const mockState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace,
      setMode: vi.fn(),
      setConversationId: vi.fn(),
      backToLanding: vi.fn(),
    }

    render(
      <AppNavContext.Provider value={mockState}>
        <Landing />
      </AppNavContext.Provider>,
    )

    await userEvent.click(screen.getByRole('button', { name: /Enter the Tavern/i }))
    expect(enterWorkspace).toHaveBeenCalledTimes(1)
  })
})
