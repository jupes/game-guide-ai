import { describe, it, expect } from 'vitest'

describe('toolchain smoke', () => {
  it('runs vitest under bun on this machine', () => {
    expect(1 + 1).toBe(2)
  })
})
