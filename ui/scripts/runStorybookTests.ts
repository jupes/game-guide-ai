/**
 * The storybook half of `ui/package.json`'s `test` script: runs the storybook
 * vitest project under the external hang guard (agent-forge-harness-w1e).
 * Deliberately nothing but the call, so there is no entry-point condition to
 * get wrong; `hangGuard.test.ts` runs this file under Bun to prove it reaches
 * the guard.
 */

import { main } from './hangGuard'

process.exit(await main())
