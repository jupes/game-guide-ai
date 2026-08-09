/**
 * zod schemas mirroring service/models.py exactly (z7fl.2) — runtime
 * validation at the API boundary, closing the compile-time-only
 * `res.json() as T` gap in api.ts. Field names and optionality match the
 * Pydantic models field-for-field, snake_case on the wire.
 */

import { z } from 'zod'

export const SourceSchema = z.object({
  book: z.string(),
  chapter: z.string().nullable(),
  section: z.string().nullable(),
  entity: z.string().nullable(),
  page: z.number().nullable(),
  snippet: z.string(),
})

export const SuggestionSchema = z.object({
  style: z.enum(['practical', 'roleplay', 'wacky']),
  text: z.string(),
})

/** Mirrors service.models.SpellComponents. */
export const SpellComponentsSchema = z.object({
  v: z.boolean().nullish(),
  s: z.boolean().nullish(),
  m: z.string().nullish(),
})

/** Mirrors service.models.SpellContent — name/description are core-required
 * on the backend (a minimal reply degrades to null before it ever reaches
 * the wire), everything else optional. */
export const SpellContentSchema = z.object({
  name: z.string(),
  description: z.string(),
  level: z.number().nullish(),
  school: z.string().nullish(),
  casting_time: z.string().nullish(),
  range: z.string().nullish(),
  duration: z.string().nullish(),
  components: SpellComponentsSchema.nullish(),
  higher_levels: z.string().nullish(),
  classes: z.array(z.string()).nullish(),
  concentration: z.boolean().nullish(),
  ritual: z.boolean().nullish(),
})

/** Mirrors service.models.Abilities — exact six keys, `.strict()` so an
 * unrecognized/misspelled key fails validation (the zod side of the
 * backend's `extra="forbid"`). */
export const AbilitiesSchema = z.object({
  str: z.number().nullish(),
  dex: z.number().nullish(),
  con: z.number().nullish(),
  int: z.number().nullish(),
  wis: z.number().nullish(),
  cha: z.number().nullish(),
}).strict()

export const StatBlockEntrySchema = z.object({
  name: z.string(),
  text: z.string(),
})

/** Mirrors service.models.StatBlockContent — name/ac/hp are core-required on
 * the backend, everything else optional. */
export const StatBlockContentSchema = z.object({
  name: z.string(),
  ac: z.number(),
  hp: z.number(),
  size: z.string().nullish(),
  type: z.string().nullish(),
  alignment: z.string().nullish(),
  ac_note: z.string().nullish(),
  hit_dice: z.string().nullish(),
  speed: z.string().nullish(),
  abilities: AbilitiesSchema.nullish(),
  saving_throws: z.string().nullish(),
  skills: z.string().nullish(),
  damage_immunities: z.string().nullish(),
  condition_immunities: z.string().nullish(),
  senses: z.string().nullish(),
  languages: z.string().nullish(),
  cr: z.union([z.string(), z.number()]).nullish(),
  xp: z.number().nullish(),
  traits: z.array(StatBlockEntrySchema).nullish(),
  actions: z.array(StatBlockEntrySchema).nullish(),
  bonus_actions: z.array(StatBlockEntrySchema).nullish(),
  reactions: z.array(StatBlockEntrySchema).nullish(),
  legendary_actions: z.array(StatBlockEntrySchema).nullish(),
})

export const ChatModeSchema = z.enum(['sage', 'spell', 'rules', 'gm'])

export const ChatResponseSchema = z.object({
  answer: z.string(),
  sources: z.array(SourceSchema),
  answerable: z.boolean(),
  suggestions: z.array(SuggestionSchema).nullish(),
  mode: ChatModeSchema.optional(),
  conversation_id: z.string().nullish(),
  spell_content: SpellContentSchema.nullish(),
  stat_block: StatBlockContentSchema.nullish(),
})

// StoredMessage is created fresh here matching today's persisted shape
// exactly — z7fl doesn't persist spell_content/stat_block (live-response-
// only, see plans/drafts/structured-content-widgets.md non-goals), so this
// schema deliberately does NOT gain the two new fields.
export const StoredMessageSchema = z.object({
  id: z.number(),
  role: z.enum(['user', 'assistant']),
  content: z.string(),
  mode: ChatModeSchema,
  created_at: z.string(),
  suggestions: z.array(SuggestionSchema).nullish(),
})

export const MessagesResponseSchema = z.object({
  conversation_id: z.string(),
  messages: z.array(StoredMessageSchema),
})

export const AttachmentSchema = z.object({
  id: z.number(),
  filename: z.string(),
  content_type: z.string(),
  chars: z.number(),
  created_at: z.string(),
})

export const AttachmentsResponseSchema = z.object({
  conversation_id: z.string(),
  attachments: z.array(AttachmentSchema),
})
