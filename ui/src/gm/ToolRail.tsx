/**
 * ToolRail (1kg.3.3) — the persistent GM tool row above the composer.
 *
 * RAIL-1: a tap **arms the composer**; it never invokes. The handoff wired
 * `onTool` straight to `runTool`, which would have fired an empty, billable
 * request on every tap — the record rejects that, and so does this component:
 * it has no idea how to run anything, only how to name a tool (X-1).
 *
 * Rail, More and the slash menu read the SAME registry (RAIL-2), so the three
 * surfaces cannot disagree: the pins come from `normalisePins`, the overflow
 * from `overflow(pins)`, and both are `REGISTRY.tools` entries.
 *
 * RAIL-10: a capability-disabled tool that is pinned keeps its place, renders
 * `aria-disabled` with its reason and still counts toward five. It is
 * `aria-disabled` rather than `disabled` on purpose — a disabled button is not
 * focusable, so its reason could never be read aloud.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import { overflow, toolById } from './registry'
import type { Tool, ToolAvailability } from './registry'
import { ToolMenu } from './ToolMenu'
import './ToolRail.css'

/** §12.2: the capability lookup failed — every tool is disabled with one message. */
export const CAPABILITY_ERROR = "Couldn't check which tools are available"

/** RAIL-13. */
export const NO_CAMPAIGN_PROMPT = 'Choose or create a campaign to use GM tools'

/** §10.2: the wide and medium layouts carry the hint; narrow does not. */
export const RAIL_HINT = '/ for all tools'

export interface ToolRailProps {
  /** RAIL-11: 0–5 unique, known tool ids, in order. */
  pins: readonly ToolId[]
  /** RAIL-10, from `toolAvailability`. */
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /** RAIL-1: arms the composer with this tool's command. Never invokes. */
  onArm: (tool: Tool) => void
  /** SLASH-15: opens the Customise rail dialog from More. */
  onCustomise?: () => void
  /**
   * RAIL-13. The campaign model is `1kg.2.5`'s; until it exists the caller
   * answers whether one is selected. Defaults to true so the rail is usable.
   */
  campaignSelected?: boolean
  onChooseCampaign?: () => void
  /** AE-58: the capability lookup failed; the rail offers Retry. */
  capabilitiesFailed?: boolean
  onRetryCapabilities?: () => void
  hint?: string | null
  className?: string
}

export function ToolRail({
  pins,
  availability,
  onArm,
  onCustomise,
  campaignSelected = true,
  onChooseCampaign,
  capabilitiesFailed = false,
  onRetryCapabilities,
  hint = RAIL_HINT,
  className,
}: ToolRailProps): React.JSX.Element {
  const id = React.useId()
  // X-8: an id the registry does not know is dropped, never guessed at.
  const pinned = pins.map((pinId) => toolById(pinId)).filter((tool): tool is Tool => tool !== undefined)
  const unpinned = overflow(pins)

  return (
    <div className={['gm-tool-rail', className].filter(Boolean).join(' ')} role="group" aria-label="GM tools">
      {!campaignSelected && (
        <p className="gm-tool-rail__notice">
          <span>{NO_CAMPAIGN_PROMPT}</span>
          {onChooseCampaign && (
            <button type="button" className="gm-tool-rail__notice-action" onClick={onChooseCampaign}>
              Choose a campaign
            </button>
          )}
        </p>
      )}

      {capabilitiesFailed && (
        <p className="gm-tool-rail__notice" role="status">
          <span>{CAPABILITY_ERROR}</span>
          {onRetryCapabilities && (
            <button type="button" className="gm-tool-rail__notice-action" onClick={onRetryCapabilities}>
              Retry
            </button>
          )}
        </p>
      )}

      <div className="gm-tool-rail__row">
        {/* One line that scrolls sideways on narrow, with More pinned outside
            it at the right (§10.2) — so the overflow is never scrolled away. */}
        <div className="gm-tool-rail__tools">
          {pinned.map((tool) => {
            const state = availability[tool.id]
            const disabled = !campaignSelected || state?.enabled !== true
            const reason = !campaignSelected ? NO_CAMPAIGN_PROMPT : (state?.enabled !== true ? (state?.reason ?? null) : null)
            const reasonId = reason ? `${id}-${tool.id}-reason` : undefined
            return (
              <button
                key={tool.id}
                type="button"
                className="gm-tool-rail__tool"
                aria-disabled={disabled || undefined}
                aria-describedby={reasonId}
                data-touch-target="true"
                onClick={() => {
                  if (!disabled) onArm(tool)
                }}
              >
                <span className="material-symbols-rounded gm-tool-rail__icon" aria-hidden="true">
                  {tool.icon}
                </span>
                {tool.label}
                {reason && (
                  <span id={reasonId} className="gm-tool-rail__sr-only">
                    {reason}
                  </span>
                )}
              </button>
            )
          })}
        </div>

        <ToolMenu
          tools={unpinned}
          availability={availability}
          onArm={onArm}
          onCustomise={onCustomise}
          disabled={!campaignSelected}
        />

        {hint && <span className="gm-tool-rail__hint">{hint}</span>}
      </div>
    </div>
  )
}
