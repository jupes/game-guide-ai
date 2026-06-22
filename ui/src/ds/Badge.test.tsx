/**
 * Badge — Behavior #9
 *
 * Tests:
 *  - Renders children as text content
 *  - All semantic tones: primary, neutral, gold, verdigris, arcane, error, nat20
 *  - NEW tone: nat1 (DS extension — not in the .d.ts; colored via --aether-nat1)
 *  - dot mode: renders as 8px circle, ignores children
 *  - Supports ALL-CAPS dice language labels (NAT 20, NAT 1, D20)
 *  - Default tone is primary
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge } from './Badge'

describe('Badge — content rendering', () => {
  it('renders numeric children', () => {
    render(<Badge>3</Badge>)
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('renders text children', () => {
    render(<Badge>Online</Badge>)
    expect(screen.getByText('Online')).toBeInTheDocument()
  })

  it('renders ALL-CAPS dice labels', () => {
    render(<Badge tone="nat20">NAT 20</Badge>)
    expect(screen.getByText('NAT 20')).toBeInTheDocument()
  })

  it('renders NAT 1 label with nat1 tone', () => {
    render(<Badge tone="nat1">NAT 1</Badge>)
    expect(screen.getByText('NAT 1')).toBeInTheDocument()
  })
})

describe('Badge — default tone', () => {
  it('defaults to primary tone', () => {
    render(<Badge data-testid="badge">3</Badge>)
    expect(screen.getByTestId('badge').className).toMatch(/badge--primary/)
  })
})

describe('Badge — semantic tones', () => {
  const tones = ['primary', 'neutral', 'gold', 'verdigris', 'arcane', 'error', 'nat20'] as const

  for (const tone of tones) {
    it(`applies ${tone} tone class`, () => {
      render(<Badge tone={tone} data-testid="badge">{tone}</Badge>)
      expect(screen.getByTestId('badge').className).toMatch(new RegExp(`badge--${tone}`))
    })
  }
})

describe('Badge — nat1 tone (extension)', () => {
  it('applies nat1 tone class', () => {
    render(<Badge tone="nat1" data-testid="badge">NAT 1</Badge>)
    expect(screen.getByTestId('badge').className).toMatch(/badge--nat1/)
  })

  it('nat1 badge renders children', () => {
    render(<Badge tone="nat1">NAT 1</Badge>)
    expect(screen.getByText('NAT 1')).toBeInTheDocument()
  })

  it('nat1 dot renders as dot', () => {
    render(<Badge tone="nat1" dot data-testid="badge" />)
    expect(screen.getByTestId('badge').className).toMatch(/badge--dot/)
  })
})

describe('Badge — dot mode', () => {
  it('renders as a dot when dot=true', () => {
    render(<Badge tone="error" dot data-testid="badge" />)
    expect(screen.getByTestId('badge').className).toMatch(/badge--dot/)
  })

  it('dot badge still gets tone class', () => {
    render(<Badge tone="error" dot data-testid="badge" />)
    expect(screen.getByTestId('badge').className).toMatch(/badge--error/)
  })

  it('nat20 dot renders', () => {
    render(<Badge tone="nat20" dot data-testid="badge" />)
    expect(screen.getByTestId('badge').className).toMatch(/badge--dot/)
    expect(screen.getByTestId('badge').className).toMatch(/badge--nat20/)
  })
})
