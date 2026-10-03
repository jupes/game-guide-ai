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
  /** §12.2: the way out of a canvas state that is not a document, and the only link to the library there. */
  backToLibrary: 'Back to library',
  /** LIB-16: an archived document stays open, read-only, under this banner. */
  archivedBanner: 'This document is archived. It is hidden from the library lists.',
  archivedRegion: 'Archived',
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

  // ── Skip links (I-17, C-14) ──
  skipLinks: 'Skip links',
  skipToConversation: 'Skip to conversation',
  skipToDocument: 'Skip to document',
} as const

/**
 * The Campaign Library (agent-forge-harness-1kg.6.4; interactions ADR section 6 and
 * 12.2). The empty and loading copy that the ADR quotes is quoted; the design lane
 * draws no library screen, so every other string is INFERRED, listed in the PR body
 * so the owner can overrule it, and lives here so `cub` can replace it without
 * touching a component.
 *
 * Titles, search text and field text are GM-private (X-7): they are ARGUMENTS here,
 * rendered and announced, never a key, an id, a storage value or a log field.
 */
export const LIBRARY_COPY = {
  title: 'Campaign Library',
  close: 'Close library',
  back: 'Back',
  tabs: 'Library categories',

  /** Per category: the tab, the list's name, and the noun the sentences use. */
  category: {
    npcs: { tab: 'NPCs', noun: 'NPCs' },
    bestiary: { tab: 'Bestiary', noun: 'the bestiary' },
    documents: { tab: 'Documents', noun: 'documents' },
    'session-log': { tab: 'Session log', noun: 'session notes' },
  },

  search: (noun: string): string => `Search ${noun}`,
  searchInvalid: "Search can't include some of those characters.",
  sort: 'Sort',
  recent: 'Recently updated',
  name: 'Name A–Z',
  show: 'Show',
  active: 'Active',
  archived: 'Archived',
  type: 'Type',
  allTypes: 'All types',

  // ── Empty states (section 12.2) ──
  empty: {
    npcs: 'No NPCs yet. Run /npc or press New.',
    bestiary: 'Nothing in the bestiary yet. Run /monster and save it, or press New.',
    documents: 'No documents yet. Press New to write one.',
    'session-log': 'No session notes yet. Run /recap at the end of a session.',
  },
  noMatch: (search: string): string => `Nothing matches "${search}"`,
  clear: 'Clear',
  archivedEmpty: (noun: string): string => `Nothing archived in ${noun}.`,

  // ── Loading and errors ──
  loading: (noun: string): string => `Loading ${noun}…`,
  loadFailed: (noun: string): string => `Couldn't load ${noun}`,
  retry: 'Retry',
  loadMore: 'Load more',
  loadingMore: 'Loading…',
  moreFailed: "Couldn't load more",

  // ── New ──
  new: 'New',
  newNamed: (typeLabel: string): string => `New ${typeLabel}`,
  creating: 'Creating…',
  created: (name: string): string => `Created ${name}`,
  createFailed: "Couldn't create the document. Nothing was saved.",
  createRefused: "Couldn't create the document.",
  storageFull: "This account's document storage is full. Archive and delete documents to make room.",
  throttled: 'Too many changes at once. Wait a moment and try again.',

  // ── The stat block dialog (LIB-12): nothing is stored until it is valid ──
  statBlockHeading: 'New stat block',
  statBlockBody: 'A stat block needs a name, an armor class and hit points.',
  statName: 'Name',
  statAc: 'Armor class',
  statHp: 'Hit points',
  statNameRequired: 'Give the stat block a name.',
  statNumberInvalid: (label: string, max: number): string => `${label} is a whole number from 0 to ${max.toLocaleString('en-US')}.`,
  statCreate: 'Create',
  cancel: 'Cancel',

  // ── Row actions: Archive (LIB-16, LIB-17) and Delete (LIB-18, SEC-40) ──
  moreActions: (title: string): string => `More actions for ${title}`,
  archive: 'Archive',
  archiveNamed: (title: string): string => `Archive ${title}`,
  archiving: 'Archiving…',
  archivedDone: (title: string): string => `Archived ${title}`,
  archiveFailed: (title: string): string => `Couldn't archive ${title}. Nothing changed.`,
  /** LIB-17, quoted. */
  liveArchiveHeading: (title: string): string => `Archive ${title}?`,
  liveArchiveBody: 'The table is seeing this. Archiving stops showing it.',
  undo: 'Undo',
  undoRegion: 'Undo archive',
  undoFailed: (title: string): string => `Couldn't undo archiving ${title}. Restore it from Archived.`,
  delete: 'Delete',
  deleteNamed: (title: string): string => `Delete ${title}`,
  deleting: 'Deleting…',
  deleteHeading: (title: string): string => `Delete ${title}?`,
  /** LIB-18, quoted. */
  deleteBody: (title: string): string => `This permanently deletes ${title} and its whole history. This can't be undone.`,
  password: 'Your password',
  passwordNeeded: 'Enter your password to delete.',
  passwordWrong: "That password didn't match. Nothing was deleted.",
  deleteFailed: (title: string): string => `Couldn't delete ${title}. Nothing was deleted.`,
  deleted: (title: string): string => `Deleted ${title}`,
  notArchived: (title: string): string => `${title} isn't archived any more. Nothing was deleted.`,

  // ── Restore ──
  restore: 'Restore',
  restoring: 'Restoring…',
  restoreNamed: (title: string): string => `Restore ${title}`,
  restored: (title: string): string => `Restored ${title}`,
  restoreFailed: (title: string): string => `Couldn't restore ${title}. Nothing changed.`,
  gone: "That document isn't available.",

  // ── Announcements ──
  found: (count: number, more: boolean): string => (more ? `${count}+ found` : count === 0 ? 'Nothing found' : `${count} found`),
  moreLoaded: (count: number): string => `${count} more loaded`,
} as const
