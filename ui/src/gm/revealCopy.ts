/**
 * revealCopy -- every user-facing string the reveal sheet and its indicators add
 * (agent-forge-harness-1kg.7.3, INFERRED I-15), in one module so the design round
 * (`cub`) can reword it without touching a component.
 *
 * The strings the interactions ADR quotes keep their wording (REVEAL-5, 13, 15, 16,
 * 23 and section 12.2); the rest are inferred and listed in the PR body.
 *
 * A title, an alias and field text are GM-private (X-7): they are ARGUMENTS here,
 * rendered and announced, never a key, an id, a storage value or a log field.
 */

export const REVEAL_COPY = {
  // ── The sheet ──
  heading: (title: string): string => `Reveal ${title}`,
  statusHidden: "The table can't see this yet.",
  statusLive: (fields: string, audience: string): string => `Revealed · ${fields} · ${audience}`,
  whoSees: 'Who sees it',
  wholeTable: 'Whole table',
  chosenPlayers: 'Chosen players',
  playersLegend: 'Players',
  loadingPlayers: 'Loading players…',
  playersFailed: "Couldn't load players",
  noPlayers: 'No seated players yet.',
  retry: 'Retry',
  choicesReset: (audience: string): string => `Choices reset for ${audience}`,
  fieldsLegend: 'Fields',
  reasonEmpty: 'Empty',
  reasonAsset: "Pictures can't be shown to players yet",
  preparing: 'Getting the document ready…',
  cancel: 'Cancel',
  stopShowing: 'Stop showing',
  revealing: 'Revealing…',
  tryAgain: 'Try again',

  // ── Audience notices ──
  partialAudience: "Some players who can see this can't be chosen here.",
  chooseAtLeastOne: 'Choose at least one player.',
  nothingChanged: 'Nothing has changed.',
  replacesTable: 'This replaces what the table is seeing now.',
  replacesWho: (who: string): string => `This replaces what ${who} is seeing now.`,
  stopsShowingToTable: 'It stops showing to the table.',
  stopsShowingTo: (who: string): string => `It stops showing to ${who}.`,

  // ── No live session (REVEAL-1) ──
  noSessionHeading: 'Start a table session',
  noSessionBody: 'Revealing needs a live table session. Starting one shows players nothing.',
  startSession: 'Start session',
  starting: 'Starting…',
  liveElsewhere: 'A session is live in another campaign. End it there first.',
  startRefused: "You can't start a table session on this account.",
  startThrottled: 'Too many tries. Try again in a moment.',
  startFailed: "Couldn't start a session.",

  // ── States ──
  unknown: 'Reveal state unknown — reconnecting',
  emptyDocument: 'Nothing to reveal yet — this document is empty',
  staleNote: 'The table is seeing an earlier version.',
  conflict: 'Reveal changed — check and confirm again',
  documentRefused: "This document can't be shown right now.",
  documentUnavailable: "This document isn't available.",
  throttled: (seconds: number | null): string =>
    seconds === null || seconds <= 0
      ? 'Too many changes at once. Try again in a moment.'
      : `Too many changes at once. Try again in ${seconds} seconds.`,
  failed: "Couldn't reveal — Try again",
  waitingForStop: 'Waiting for Stop showing to finish…',
  sealFailed: "Aetheril can't reach its library right now. Nothing was lost.",

  // ── Effects ──
  effectNone: 'Reveal',
  effectUpdate: 'Update',
  revealToTable: 'Reveal to the table',
  revealTo: (who: string): string => `Reveal to ${who}`,
  revealAndReplace: 'Reveal and replace',
  moveToTable: 'Move to the table',
  moveTo: (who: string): string => `Move to ${who}`,
  tableName: 'the table',
  toTheTable: 'to the table',
  toWho: (who: string): string => `to ${who}`,
  playersCount: (count: number): string => `${count} players`,

  // ── Announcements (the one status node) ──
  shownTo: (who: string): string => `Shown to ${who}`,
  updated: (audience: string): string => `Updated what ${audience} sees`,
  movedTo: (audience: string): string => `Moved to ${audience}`,
  stopped: (title: string): string => `Stopped showing ${title}`,
  stopRetrying: "Couldn't stop showing — retrying. The table may still see it.",
  thisDocument: 'this document',
  stopInvalid: (title: string): string => `Couldn't stop showing ${title}. The table may still see it.`,

  // ── The canvas ──
  checking: 'Checking what the table sees…',
  checkingBadge: 'CHECKING',
  revealedBadge: 'REVEALED',
  waitingNote: 'Waiting until you confirm the seat',
  stopRetryingMessage: "Couldn't stop showing — retrying",
  tableCanSee: 'The table can see this',
  whoCanSee: (who: string): string => `${who} can see this`,
  waitingToShow: (who: string): string => `Waiting to show ${who}`,

  // ── The workspace indicator (PR-2, REVEAL-14) ──
  indicatorLabel: 'What the table sees',
  indicatorListLabel: 'Everything the table is shown',
  stopping: 'Stopping…',
  indicatorRevealed: 'Revealed',
  indicatorUnknown: 'Reveal state unknown — reconnecting',
  indicatorTable: 'table',
  indicatorMore: (count: number): string => `+${count} more`,
  aDocument: 'a document',
  showList: 'Show everything that is revealed',
  hideList: 'Hide the list',
  stopAll: (count: number | null): string => (count === null ? 'Stop all' : `Stop all (${count})`),
  stopShowingTitle: (title: string): string => `Stop showing ${title}`,
  stopShort: 'Stop',
  everything: 'everything',
  openSheetFor: (title: string): string => `Open the reveal sheet for ${title}`,
  goToGm: (title: string): string => `Go to the GM channel for ${title}`,
  earlierVersionShort: 'Table is seeing an earlier version',
  updateEllipsis: 'Update…',
  stopFailedShort: "Couldn't stop showing — retrying",
  documentNotOpened: "Couldn't open that document.",

  // ── REVEAL-8: Use latest version (PR-2) ──
  useLatest: 'Use latest version',
  loadingLatest: 'Loading the latest version…',
  latestFailed: "Couldn't load the latest version. Nothing changed.",
  latestHeading: 'What changes for the table',
  latestOld: 'The table sees',
  latestNew: 'Latest',
  latestEmpty: 'Empty',
  keepPinned: 'Keep what the table sees',
  latestNoTicked: 'Tick a field to compare it with the latest version.',

  // ── REVEAL-16: the Login screen after a 401 during a live reveal (PR-2) ──
  stillSees: (titles: readonly string[]): string => {
    if (titles.length === 1) return `The table can still see ${titles[0]}`
    if (titles.length === 2) return `The table can still see ${titles[0]} and ${titles[1]}`
    return `The table can still see ${titles[0]}, ${titles[1]} and ${titles.length - 2} more`
  },
} as const

/** Used where a seat id names nobody this client knows (removed, or not yet listed). */
export const WHO_UNKNOWN = 'a player'
