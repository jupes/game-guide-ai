/**
 * Pure parser for inline dice notation found in LLM answers, e.g.
 *   "1d20+5=18"  ·  "2d6 - 3 = 9"  ·  "1d20 = 14"
 *
 * Extracted from ChatPane so it can be unit-tested without rendering.
 *
 * Mapping contract (consumed by the DS DiceRoll component, which renders
 * `total = value + modifier`):
 *   - die       → number of sides (the N in dNN)
 *   - modifier  → SIGNED flat modifier (− is honored, not dropped)
 *   - value     → the summed rolled face(s) = total − modifier, so that
 *                 DiceRoll re-derives the original total the model reported.
 *   - total     → the model's reported final number (kept for callers/tests).
 */

export interface DiceMatch {
  /** Die sides (the NN in dNN). */
  die: number
  /** Rolled value the DiceRoll chip should display (= total − modifier). */
  value: number
  /** Signed flat modifier (negative when the notation subtracts). */
  modifier: number
  /** The final total the model reported after the equals sign. */
  total: number
}

// count "d" sides (sign modifier)? = total
const DICE_RE = /(\d+)d(\d+)\s*(?:([+−-])\s*(\d+))?\s*=\s*(\d+)/i

/**
 * Parse the first dice expression in `text`, or return null when there is none.
 * Never throws.
 */
export function parseDiceNotation(text: string): DiceMatch | null {
  const match = DICE_RE.exec(text)
  if (!match) return null

  const die = Number.parseInt(match[2], 10)
  const total = Number.parseInt(match[5], 10)

  // Signed modifier: capture group 3 is the sign (+, -, or U+2212 minus).
  const magnitude = match[4] ? Number.parseInt(match[4], 10) : 0
  const negative = match[3] === '-' || match[3] === '−'
  const modifier = negative ? -magnitude : magnitude

  // value is the summed face(s) so that value + modifier === total.
  const value = total - modifier

  if ([die, value, modifier, total].some((n) => Number.isNaN(n))) return null
  return { die, value, modifier, total }
}
