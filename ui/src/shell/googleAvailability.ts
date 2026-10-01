/**
 * useGoogleAvailable -- does this service offer Google sign-in (lvs7).
 *
 * `false` until the service has said `{"available": true}`, and `false` for
 * good on a 404 (the feature is off), any failure, or a network error. So the
 * button is never drawn on a guess, and an unconfigured deployment shows
 * nothing at all -- no flash, no disabled stub.
 */

import { useEffect, useState } from 'react'
import * as api from '../api'

export function useGoogleAvailable(): boolean {
  const [available, setAvailable] = useState(false)
  useEffect(() => {
    let live = true
    void api.googleAvailable().then((answer) => {
      if (live) setAvailable(answer)
    })
    return () => {
      live = false
    }
  }, [])
  return available
}
