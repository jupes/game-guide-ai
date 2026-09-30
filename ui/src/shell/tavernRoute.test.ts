/**
 * tavernRoute.test.ts -- the /tavern row in the client route table
 * (agent-forge-harness-74j, brief section 7.1, T-1).
 *
 * A dedicated file, not an edit to `routes.test.ts`: that file stays
 * byte-identical (brief section 4's file map).
 */
import { describe, it, expect } from 'vitest'
import { routeForPath, pathForScreen, startScreen } from './routes'

describe('the /tavern route (74j, T-1)', () => {
  it('routes /tavern to the tavern screen on a cold load, and no neighbour of it', () => {
    expect(routeForPath('/tavern')).toEqual({ path: '/tavern', screen: 'tavern', coldLoad: 'restore' })
    expect(pathForScreen('tavern')).toBe('/tavern')
    expect(startScreen('/tavern')).toEqual({ screen: 'tavern', replacePath: null })
    expect(startScreen('/tavern/')).toEqual({ screen: 'landing', replacePath: '/' })
    expect(startScreen('/taverns')).toEqual({ screen: 'landing', replacePath: '/' })
  })
})
