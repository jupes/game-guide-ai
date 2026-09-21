import { runCompose } from './compose'
import { EXTERNAL_STACK_URL } from './stack'

export default function globalTeardown(): void {
  // Mirror global setup: tear down only the stack this run brought up.
  if (EXTERNAL_STACK_URL !== null) return
  runCompose('down', '--volumes', '--remove-orphans')
}
