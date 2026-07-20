import * as React from 'react'
import { useAppNav } from './shell/AppNav'
import { Landing } from './shell/Landing'
import { WorkspaceShell } from './shell/WorkspaceShell'
import { ProfilePage } from './shell/ProfilePage'

export default function App(): React.JSX.Element {
  const { screen } = useAppNav()
  if (screen === 'landing') return <Landing />
  if (screen === 'profile') return <ProfilePage />
  return <WorkspaceShell />
}
