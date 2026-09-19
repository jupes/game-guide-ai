/**
 * AssistantDocumentLink — the lane's row for a document that was saved
 * (1kg.3.2, decision CANVAS-3).
 *
 * Three things the handoff's version does not do.
 *
 * It reads `<kind> · saved to <category>` rather than "open in canvas" as a
 * subtitle, because CANVAS-3's point is that the document is *already stored*
 * (X-6, "save before link") and the canvas is where the GM may go next, not
 * where the result has gone. The action is named separately and exactly:
 * **Open in canvas**.
 *
 * Nothing opens by itself. CANVAS-1's implicit open belongs to the surface that
 * owns the canvas and knows the layout and whether the result arrived live;
 * this component only ever calls `onOpen`, and only from an activation.
 *
 * The labels come from the registry, never from the wire: a document type this
 * bundle does not know renders nothing here and the lane shows its neutral
 * placeholder instead (X-8 — unknown is not NPC).
 */

import * as React from 'react'
import type { DocumentLink } from './contracts'
import { LANE_COPY, documentLinkMeta } from './laneState'
import './AssistantDocumentLink.css'

export interface AssistantDocumentLinkProps {
  link: DocumentLink
  /** Activating the row. Required: a link with no destination is a dead end (STATE-2). */
  onOpen: (link: DocumentLink) => void
  className?: string
}

export function AssistantDocumentLink({ link, onOpen, className }: AssistantDocumentLinkProps): React.JSX.Element | null {
  const meta = documentLinkMeta(link)
  // X-8: a type this bundle does not know renders nothing rather than guessing.
  if (meta === null) return null

  return (
    <button
      type="button"
      className={['assistant-document-link', className].filter(Boolean).join(' ')}
      onClick={() => onOpen(link)}
    >
      <span className="material-symbols-rounded assistant-document-link__icon" aria-hidden="true">
        description
      </span>
      <span className="assistant-document-link__text">
        {/* The title is GM-private text (X-7): it is rendered, and it never
            becomes an id, a class, a data attribute or a storage key. */}
        <span className="assistant-document-link__title">{link.title}</span>
        <span className="assistant-document-link__meta">{meta}</span>
      </span>
      <span className="assistant-document-link__action">{LANE_COPY.openInCanvas}</span>
      <span className="material-symbols-rounded assistant-document-link__chevron" aria-hidden="true">
        dock_to_right
      </span>
    </button>
  )
}
