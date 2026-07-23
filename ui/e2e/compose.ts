import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const e2eDirectory = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(e2eDirectory, '..', '..')
const composeFile = path.join(repositoryRoot, 'docker-compose.e2e.yml')
const projectName = 'game-guide-ai-e2e'

export function runCompose(...args: string[]): void {
  const result = spawnSync(
    'docker',
    [
      'compose',
      '--project-name',
      projectName,
      '--file',
      composeFile,
      ...args,
    ],
    {
      cwd: repositoryRoot,
      stdio: 'inherit',
      shell: false,
    },
  )
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`docker compose ${args.join(' ')} exited ${result.status}`)
  }
}
