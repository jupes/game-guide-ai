/**
 * workbenchCopy -- every user-facing string the Workbench shell adds
 * (agent-forge-harness-1kg.6.3), in one module so the design round (`cub`)
 * can replace the wording without touching a component.
 *
 * The strings that come from the interactions ADR are quoted by id; the rest
 * are INFERRED (the design lane draws nothing for the Workbench screen yet) and
 * listed in the PR body so the owner can overrule them.
 *
 * Document titles are GM-private (X-7): a title is an ARGUMENT here, rendered
 * and announced, never a key, an id, a storage value or a log field.
 */

export const WORKBENCH_COPY = {
  // ── The canvas column ──
  /** Visible while a document loads; not a live region (A-29). */
  opening: (title: string | null): string => (title === null ? 'Opening the document…' : `Opening ${title}…`),
  /** CANVAS-31, STATE-5: 403 and 404 share one state that never says which. */
  unavailableHeading: "This document isn't available",
  unavailableBody: 'It may have been deleted, or you may not have access to it.',
  /** X-8: a document from a newer schema or an unknown type. */
  unsupportedHeading: 'This document was made by a newer version of Aetheril.',
  failedHeading: (title: string | null): string =>
    title === null ? "Couldn't open the document" : `Couldn't open ${title}`,
  /** STATE-5: 5xx, 503 and a network failure. */
  failedBody: "Aetheril can't reach its library right now. Nothing was lost.",
  retry: 'Retry',
  close: 'Close',

  // ── The loss guard (§5.3, CANVAS-16) ──
  guardHeading: (title: string): string => `You have unsaved changes to ${title}`,
  guardBody: "Your latest edits couldn't be saved.",
  guardBodyRetryable: "Your latest edits couldn't be saved yet. Aetheril can try again.",
  keepEditing: 'Keep editing',
  trySavingAgain: 'Try saving again',
  savingAgain: 'Saving…',
  discardChanges: 'Discard changes',
  /** STATE-6, AE-19: the switch is refused while offline, with no dialog. */
  offlineRefusal: "You're offline — changes aren't saved. Keep this tab open.",
  /** A document that cannot load never makes the guard discard anything (§5.3). */
  openFailedNothingChanged: (title: string | null): string =>
    `Couldn't open ${title ?? 'the document'}. Nothing changed.`,
  /** Used when a late failure lands while focus is elsewhere and the panel is not read out. */
  untitled: (typeLabel: string): string => `Untitled ${typeLabel}`,
  thisDocument: 'this document',

  // ── The Workbench view switch (LAYOUT-5, I-4) ──
  viewSwitchLabel: 'Workbench view',
  viewChat: 'Chat',
  viewCanvas: 'Canvas',

  // ── The navigation rail (LAYOUT-7, I-3, C-9) ──
  railLabel: 'Navigation rail',
  openNavigation: 'Open navigation',
  campaignDocuments: 'Campaign documents',

  // ── Campaign documents in the nav column (I-2, 2.7) ──
  documentsLoading: 'Loading documents…',
  documentsEmpty: 'No documents yet.',
  documentsError: "Couldn't load documents",
  documentsRetry: 'Retry',

  // ── Skip links (I-17, C-14) ──
  skipToConversation: 'Skip to conversation',
  skipToDocument: 'Skip to document',
} as const
