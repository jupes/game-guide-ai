import { runCompose } from './compose'

export default function globalTeardown(): void {
  runCompose('down', '--volumes', '--remove-orphans')
}
