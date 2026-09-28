import { runCompose } from './compose'
import { EXTERNAL_STACK_URL } from './stack'

export default function globalSetup(): void {
  // Someone else owns the stack (E2E_BASE_URL): don't build, don't start, and
  // above all don't adopt a Compose project this process didn't create.
  if (EXTERNAL_STACK_URL !== null) return
  runCompose('up', '--build', '--detach', '--wait', '--wait-timeout', '180')
}
