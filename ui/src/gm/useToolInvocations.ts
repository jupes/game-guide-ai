import * as React from 'react'
import type { ToolInvocation, ToolInvocationRequest } from './contracts'
import type { LaneActionId } from './laneState'
import { emptyPendingWork, intentFor, overdue, reducePendingWork, type PendingEvent } from './pendingWork'
import { cancelTool, checkTool, startTool } from './toolInvocationApi'

const POLL_MS = 5_000

export function useToolInvocations(campaignId: string | null) {
  const state = React.useRef(emptyPendingWork())
  const [work, setWork] = React.useState(emptyPendingWork)
  const checking = React.useRef(new Set<string>())
  const cancelling = React.useRef(new Set<string>())
  const starting = React.useRef(new Set<string>())

  const dispatch = React.useCallback((event: PendingEvent) => {
    const next = reducePendingWork(state.current, event)
    if (next === state.current) return
    state.current = next
    setWork(next)
  }, [])

  const submit = React.useCallback(async (request: ToolInvocationRequest) => {
    const id = request.invocation_id
    if (starting.current.has(id)) return null
    starting.current.add(id)
    dispatch({ type: 'submitted', request, at: Date.now() })
    try {
      const result = await startTool(request)
      switch (result.kind) {
        case 'ok':
          dispatch({ type: 'answered', invocation: result.invocation })
          break
        case 'refused':
          dispatch({ type: 'refused', invocationId: id, error: result.error, at: Date.now() })
          break
        case 'lost':
          dispatch({ type: 'lost', invocationId: id })
          break
      }
      return result
    } finally {
      starting.current.delete(id)
    }
  }, [dispatch])

  const check = React.useCallback(async (campaign: string, id: string) => {
    if (checking.current.has(id)) return
    checking.current.add(id)
    const before = state.current.entries.get(id)
    try {
      const result = await checkTool(campaign, id)
      if (result.kind === 'ok') dispatch({ type: 'answered', invocation: result.invocation })
      if (result.kind === 'refused' && result.error.code === 'not_found'
        && before?.lost && before.invocation === null && state.current.entries.get(id) === before
        && !starting.current.has(id)) {
        // There is no stored attempt. A deliberate retry still uses this id.
        dispatch({
          type: 'refused',
          invocationId: id,
          error: { code: 'tool_not_found', retryable: true, message: 'The tool run could not be found.' },
          at: Date.now(),
        })
      }
    } finally {
      checking.current.delete(id)
    }
  }, [dispatch])

  const action = React.useCallback((
    request: ToolInvocationRequest,
    invocation: ToolInvocation,
    laneAction: LaneActionId,
  ) => {
    const id = request.invocation_id
    dispatch({ type: 'adopt', request, invocation, at: Date.now() })
    const intent = intentFor(state.current, id, laneAction, invocation)
    if (intent === null || intent.type === 'arm') return intent
    if (intent.type === 'start') void submit(intent.request)
    if (intent.type === 'status') void check(request.campaign_id, id)
    if (intent.type === 'cancel' && !cancelling.current.has(id)) {
      cancelling.current.add(id)
      dispatch({ type: 'cancel-requested', invocationId: id })
      void cancelTool(request.campaign_id, id).then((result) => {
        cancelling.current.delete(id)
        if (result.kind === 'ok') dispatch({ type: 'answered', invocation: result.invocation })
        else dispatch({ type: 'cancel-failed', invocationId: id })
      })
    }
    return intent
  }, [check, dispatch, submit])

  const adopt = React.useCallback((request: ToolInvocationRequest, invocation: ToolInvocation) => {
    dispatch({ type: 'adopt', request, invocation, at: Date.now() })
  }, [dispatch])

  const watch = work.capBlockedUntil !== null || [...work.entries.values()].some((entry) =>
    entry.awaiting || entry.invocation?.status === 'working')
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    if (!watch) return
    const inspect = () => {
      const current = Date.now()
      for (const id of overdue(state.current, current)) dispatch({ type: 'lost', invocationId: id })
      for (const [id, entry] of state.current.entries) {
        const pending = entry.awaiting || (entry.refusal === null && entry.invocation?.status === 'working')
        if (pending && (entry.lost || entry.cancelling)) void check(entry.request.campaign_id, id)
      }
      if (campaignId !== null && state.current.capBlockedUntil !== null && current < state.current.capBlockedUntil) {
        for (const id of state.current.capInFlight) {
          if (!state.current.entries.has(id)) void check(campaignId, id)
        }
      }
      dispatch({ type: 'cap-check', at: current })
      setNow(current)
    }
    const timer = window.setInterval(inspect, POLL_MS)
    return () => window.clearInterval(timer)
  }, [campaignId, check, dispatch, watch])

  return { work, now, submit, action, adopt }
}

export type ToolInvocations = ReturnType<typeof useToolInvocations>
