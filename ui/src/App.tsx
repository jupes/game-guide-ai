import * as React from 'react'
import { useAppNav } from './shell/AppNav'
import { Landing } from './shell/Landing'
import { WorkspaceShell } from './shell/WorkspaceShell'

export default function App(): React.JSX.Element {
  const { screen } = useAppNav()
  return screen === 'landing' ? <Landing /> : <WorkspaceShell />
}
