/**
 * ProfilePage — the current user's profile (swe1.7).
 *
 * Edits the locally-stubbed identity: display name, avatar tone, and player/DM
 * role (via the shared useRoleToggle so the gm→sage fallback stays in one
 * place). Real accounts — username, email/password, per-user server storage,
 * and social fields — arrive with the pilot-auth work (x5bz.2); those are
 * documented here rather than invented.
 */

import * as React from 'react'
import { Avatar, type AvatarTone } from '../ds/Avatar'
import { Card } from '../ds/Card'
import { TextField } from '../ds/TextField'
import { Switch } from '../ds/Switch'
import { Button } from '../ds/Button'
import { useCurrentUser } from './currentUser'
import { useAppNav } from './AppNav'
import { useRoleToggle } from './useRoleToggle'
import './ProfilePage.css'

const TONE_OPTIONS: readonly { tone: AvatarTone; label: string }[] = [
  { tone: 'gold', label: 'Gold' },
  { tone: 'ember', label: 'Ember' },
  { tone: 'verdigris', label: 'Verdigris' },
  { tone: 'arcane', label: 'Arcane' },
]

// Fields that only make sense once real accounts exist (x5bz.2).
const DEFERRED_FIELDS = ['Username', 'Email & password', 'Status & game preferences']

export function ProfilePage(): React.JSX.Element {
  const { user, setDisplayName, setAvatarTone } = useCurrentUser()
  const { backToWorkspace } = useAppNav()
  const toggleRole = useRoleToggle()
  const tone = user.avatarTone ?? 'gold'

  return (
    <div className="profile-page">
      <Card className="profile-page__card">
        <header className="profile-page__header">
          <Avatar name={user.displayName} tone={tone} size={72} />
          <h1 className="profile-page__title">Profile</h1>
        </header>

        <TextField
          label="Display name"
          value={user.displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          fullWidth
        />

        <fieldset className="profile-page__tones">
          <legend className="profile-page__legend">Avatar color</legend>
          <div className="profile-page__tone-row">
            {TONE_OPTIONS.map(({ tone: t, label }) => (
              <button
                key={t}
                type="button"
                className={
                  t === tone
                    ? 'profile-page__tone profile-page__tone--selected'
                    : 'profile-page__tone'
                }
                aria-pressed={t === tone}
                aria-label={`${label} avatar`}
                onClick={() => setAvatarTone(t)}
              >
                <Avatar name={user.displayName} tone={t} size={40} />
              </button>
            ))}
          </div>
        </fieldset>

        <div className="profile-page__role">
          <span id="profile-role-label">Dungeon Master</span>
          <Switch
            checked={user.role === 'dm'}
            onChange={toggleRole}
            ariaLabel="Dungeon Master role"
          />
        </div>

        <section className="profile-page__deferred" aria-label="Awaiting sign-in">
          <h2 className="profile-page__deferred-title">Available once you have an account</h2>
          <ul className="profile-page__deferred-list">
            {DEFERRED_FIELDS.map((field) => (
              <li key={field}>{field}</li>
            ))}
          </ul>
          <p className="profile-page__deferred-note">
            Your profile is stored locally in this browser for now — real, private accounts arrive with sign-in.
          </p>
        </section>

        <Button
          variant="filled"
          icon="arrow_back"
          onClick={backToWorkspace}
          className="profile-page__back"
        >
          Back to chat
        </Button>
      </Card>
    </div>
  )
}
