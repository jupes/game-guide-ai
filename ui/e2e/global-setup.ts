import { runCompose } from './compose'

export default function globalSetup(): void {
  runCompose('up', '--build', '--detach', '--wait', '--wait-timeout', '180')
}
