// Live Session Assistant cost model calculator. All prices USD, list prices accessed 2026-09-16
// (see docs/forge/research/live-session-assistant-cost-model.md for sources). Assumptions are explicit.

type Scenario = 'low' | 'expected' | 'stress'
const S: Scenario[] = ['low', 'expected', 'stress']
const r = (n: number, d = 4) => Number(n.toFixed(d))
const usd = (n: number, d = 2) => `$${n.toFixed(d)}`

// ── Listening-hour assumptions (timed/session listening with client VAD + utterance uploads)
const speechFraction = { low: 0.6, expected: 0.675, stress: 0.75 } // research §3.3 (unit costs)
const vadPadding = { low: 1.05, expected: 1.08, stress: 1.12 } // pre/post roll + short-segment overhead
const sttPerAudioMin = {
  low: 0.10 / 60, // Soniox stt-async-v5 $0.10/h
  expected: 0.0045, // OpenAI gpt-transcribe $0.27/h
  stress: (0.258 + 0.078) / 60, // Deepgram Nova-3 batch $0.258/h + keyterm $0.078/h
}
const extractionPerHour = { low: 0.023, expected: 0.045, stress: 0.327 } // gpt-4o-mini w/ lexicon gating 50% | gpt-4o-mini cached prefix | gpt-5.4-mini uncached
const cardsPerHour = { low: 0.005, expected: 0.009, stress: 0.14 } // gpt-4o-mini 10 cards | same | gpt-5.6-terra-class 10 cards
// Pre-storage classifier (out-of-game and personal data, Phase 3+): one call per 20 s hold-back micro-batch
// (at most 180 calls/h), ~570 input / 30 output tokens: Gemini 2.5 Flash-Lite | gpt-4o-mini | gpt-5.4-mini.
const classifierCalls = 180
const classifierPerHour = {
  low: classifierCalls * (570 * 0.10 + 30 * 0.40) / 1e6,
  expected: classifierCalls * (570 * 0.15 + 30 * 0.60) / 1e6,
  stress: classifierCalls * (570 * 0.75 + 30 * 4.50) / 1e6,
}
const embeddingsPerHour = { low: 0.0001, expected: 0.0001, stress: 0.0002 }
const aiOverhead = { low: 0.05, expected: 0.10, stress: 0.30 } // retries, validation failures, variance
const computePerHour = { low: 0.002, expected: 0.004, stress: 0.016 } // request-based chunk handling (shared instances vs none)
const realtimePerHour = { low: 0.005, expected: 0.02, stress: 0.095 } // participant indicator/card connections; stress = one instance-hour alone
const storageKmsLogPerHour = { low: 0.0005, expected: 0.001, stress: 0.003 }
const imagesPerHour = { low: 0.0002, expected: 0.0004, stress: 0.0036 } // 150 KB derivatives × 10/20/200 views at $0.12/GiB

export function listeningHour(s: Scenario) {
  const billedMin = 60 * speechFraction[s] * vadPadding[s]
  const stt = billedMin * sttPerAudioMin[s]
  const ai = extractionPerHour[s] + cardsPerHour[s] + classifierPerHour[s] + embeddingsPerHour[s]
  const overhead = (stt + ai) * aiOverhead[s]
  const infra = computePerHour[s] + realtimePerHour[s] + storageKmsLogPerHour[s] + imagesPerHour[s]
  return { billedMin: r(billedMin, 1), stt: r(stt), classifier: r(classifierPerHour[s]), ai: r(ai), overhead: r(overhead), infra: r(infra), total: r(stt + ai + overhead + infra) }
}

// Streaming alternatives priced on wall-clock session time (Phase 5 option) — STT only.
export const streamingSttPerHour = {
  'Soniox stt-rt-v5 (diarization incl.)': 0.12,
  'AssemblyAI Universal-Streaming': 0.15,
  'AssemblyAI Universal-Streaming + diarization': 0.27,
  'ElevenLabs Scribe v2 Realtime': 0.39,
  'Deepgram Nova-3 streaming (regular)': 0.462,
  'AWS Transcribe streaming (diarization + vocab incl.)': 0.60,
  'Azure real-time': 1.00,
  'OpenAI gpt-live-transcribe': 1.02,
}

// ── Push-to-talk clip
const clipSeconds = { low: 8, expected: 12, stress: 25 }
const clipExtraction = { low: 0.0004, expected: 0.000525, stress: 0.003 } // 2,500 in / 250 out tokens: gpt-4o-mini | gpt-4o-mini | gpt-5.4-mini
const clipComposeShare = { low: 0.3, expected: 0.5, stress: 1.0 }
const clipComposeCost = { low: 0.0006, expected: 0.00069, stress: 0.0108 } // 3,000 in / 400 out: gpt-4o-mini | gpt-4o-mini | gpt-5.6-terra
export function pttClip(s: Scenario) {
  const stt = (clipSeconds[s] * vadPadding[s] / 60) * sttPerAudioMin[s]
  const ai = clipExtraction[s] + clipComposeShare[s] * clipComposeCost[s] + classifierPerHour[s] / classifierCalls // one classifier call per clip
  const total = (stt + ai) * (1 + aiOverhead[s]) + 0.00005 // request compute
  return { stt: r(stt, 5), ai: r(ai, 5), total: r(total, 5) }
}

// Transcript upload (text only): extraction over text at listening-hour extraction rates per transcript hour.
export function transcriptUploadHour(s: Scenario) {
  return r((extractionPerHour[s] + cardsPerHour[s] + classifierPerHour[s]) * (1 + aiOverhead[s]) + storageKmsLogPerHour[s])
}

// ── Plans and fees
const fees = (price: number) => price * (0.029 + 0.007 + 0.005) + 0.30 // card 2.9% + $0.30, Billing 0.7%, Tax 0.5% (billing plan's conservative assumption)
const baseChatCogs = { low: 1.0, expected: 1.5, stress: 3.0 } // from subscription-billing plan (target $1.50, hard cap $3, stress $5 incl. future media)

interface Usage { clips: number; listeningHours: number; uploadHours: number }
export const personas: Record<string, Usage> = {
  'Base — light GM (PTT only)': { clips: 40, listeningHours: 0, uploadHours: 0 },
  'Base — typical GM': { clips: 100, listeningHours: 1, uploadHours: 2 },
  'Premium — typical GM': { clips: 150, listeningHours: 8, uploadHours: 2 },
  // Heavy weekly 4 h GM wants 17.3 h/month; usage is capped at the plan allowance.
  'Premium $25 (12 h) — heavy GM at cap': { clips: 150, listeningHours: 12, uploadHours: 0 },
  'Premium $25 (12 h) — power GM at cap': { clips: 400, listeningHours: 12, uploadHours: 4 },
  'Premium $29 (20 h) — heavy weekly 4 h GM': { clips: 150, listeningHours: 17.3, uploadHours: 0 },
  'Premium $29 (20 h) — power GM at cap': { clips: 400, listeningHours: 20, uploadHours: 4 },
}

export function liveCogs(u: Usage, s: Scenario) {
  return r(u.clips * pttClip(s).total + u.listeningHours * listeningHour(s).total + u.uploadHours * transcriptUploadHour(s))
}

export function contribution(price: number, cogs: number) {
  const f = fees(price)
  const c = price - f - cogs
  return { fees: r(f), contribution: r(c), margin: r(c / price, 3) }
}

// ── Output
const lines: string[] = []
const log = (s = '') => lines.push(s)

log('## Per listening hour (utterance uploads)')
log('| Scenario | Billed audio min | STT | Extraction + cards + classifier + embeddings (classifier) | AI/STT overhead | Infra (compute, realtime, storage/KMS/logs, images) | **Total / listening hour** |')
log('| --- | --- | --- | --- | --- | --- | --- |')
for (const s of S) { const h = listeningHour(s); log(`| ${s} | ${h.billedMin} | ${usd(h.stt, 3)} | ${usd(h.ai, 3)} (${usd(h.classifier, 3)}) | ${usd(h.overhead, 3)} | ${usd(h.infra, 3)} | **${usd(h.total, 3)}** |`) }
log()
log('## Push-to-talk clip')
log('| Scenario | Clip speech (s) | STT | Extraction + composition | **Total / clip** | 100 clips |')
log('| --- | --- | --- | --- | --- | --- |')
for (const s of S) { const c = pttClip(s); log(`| ${s} | ${clipSeconds[s]} | ${usd(c.stt, 5)} | ${usd(c.ai, 5)} | **${usd(c.total, 4)}** | ${usd(c.total * 100)} |`) }
log()
log('## Transcript upload (text) per transcript hour')
log(`| low ${usd(transcriptUploadHour('low'), 3)} | expected ${usd(transcriptUploadHour('expected'), 3)} | stress ${usd(transcriptUploadHour('stress'), 3)} |`)
log()
log('## 4-hour session, STT only, streaming (wall clock) vs utterance uploads')
log('| Option | 4 h STT cost |'); log('| --- | --- |')
for (const [k, v] of Object.entries(streamingSttPerHour)) log(`| ${k} | ${usd(v * 4)} |`)
for (const s of S) log(`| Utterance uploads (${s}) | ${usd(listeningHour(s).stt * 4)} |`)
log()
log('## Persona monthly live COGS (excludes base chat COGS)')
log('| Persona | Clips | Listening h | Upload h | Low | Expected | Stress |')
log('| --- | --- | --- | --- | --- | --- | --- |')
for (const [name, u] of Object.entries(personas)) log(`| ${name} | ${u.clips} | ${u.listeningHours} | ${u.uploadHours} | ${usd(liveCogs(u, 'low'))} | ${usd(liveCogs(u, 'expected'))} | ${usd(liveCogs(u, 'stress'))} |`)
log()

log('## Plan contribution per subscriber (base chat COGS included; fees = 4.1% + $0.30)')
log('| Plan / price | Persona | Scenario | Total COGS | Fees | Contribution | Margin |')
log('| --- | --- | --- | --- | --- | --- | --- |')
const planRows: [string, number, string, number | null][] = [
  ['Base $15', 15, 'Base — light GM (PTT only)', 3],
  ['Base $15', 15, 'Base — typical GM', 3],
  ['Premium $25 (12 h)', 25, 'Premium — typical GM', 12],
  ['Premium $25 (12 h)', 25, 'Premium $25 (12 h) — heavy GM at cap', 12],
  ['Premium $25 (12 h)', 25, 'Premium $25 (12 h) — power GM at cap', 12],
  ['Premium $29 (20 h)', 29, 'Premium $29 (20 h) — heavy weekly 4 h GM', 12],
  ['Premium $29 (20 h)', 29, 'Premium $29 (20 h) — power GM at cap', 12],
  ['Premium $19 (12 h)', 19, 'Premium $25 (12 h) — heavy GM at cap', 12],
]
for (const [plan, price, persona, cap] of planRows) for (const s of S) {
  const raw = liveCogs(personas[persona], s) + baseChatCogs[s]
  const capped = cap !== null ? Math.min(raw, cap) : raw
  const c = contribution(price, capped)
  log(`| ${plan} | ${persona} | ${s} | ${usd(capped)}${capped < raw ? ` (cap; raw ${usd(raw)})` : ''} | ${usd(c.fees)} | ${usd(c.contribution)} | ${(c.margin * 100).toFixed(1)}% |`)
}
log()

// Portfolio: 1,000 paying live-enabled subscribers
log('## Portfolio (1,000 subscribers): 85% base, 15% premium at $25 with a 12 h allowance')
const mix: [string, number, number, number][] = [
  // persona, share, price, cap
  ['Base — light GM (PTT only)', 0.55, 15, 3],
  ['Base — typical GM', 0.30, 15, 3],
  ['Premium — typical GM', 0.08, 25, 12],
  ['Premium $25 (12 h) — heavy GM at cap', 0.05, 25, 12],
  ['Premium $25 (12 h) — power GM at cap', 0.02, 25, 12],
]
log('| Scenario | Revenue | Fees | COGS | Contribution | Margin | Incremental live COGS |')
log('| --- | --- | --- | --- | --- | --- | --- |')
for (const s of S) {
  let rev = 0, fee = 0, cogs = 0, live = 0
  for (const [persona, share, price, cap] of mix) {
    const n = 1000 * share
    const l = liveCogs(personas[persona], s)
    const total = Math.min(l + baseChatCogs[s], cap)
    rev += n * price; fee += n * fees(price); cogs += n * total; live += n * Math.max(0, total - baseChatCogs[s])
  }
  const c = rev - fee - cogs
  log(`| ${s} | ${usd(rev, 0)} | ${usd(fee, 0)} | ${usd(cogs, 0)} | ${usd(c, 0)} | ${((c / rev) * 100).toFixed(1)}% | ${usd(live, 0)} |`)
}
log()

// Incremental premium economics and break-even on one-off gate costs (placeholders, not quotes)
log('## Premium upgrade break-even (incremental over base $15, heavy persona at the plan allowance)')
log('| Premium price (allowance) | Scenario | Incremental revenue | Incremental fees | Incremental COGS | Incremental contribution / upgrade |')
log('| --- | --- | --- | --- | --- | --- |')
const upgradeRows: [number, string, string][] = [
  [19, '12 h', 'Premium $25 (12 h) — heavy GM at cap'],
  [25, '12 h', 'Premium $25 (12 h) — heavy GM at cap'],
  [29, '20 h', 'Premium $29 (20 h) — heavy weekly 4 h GM'],
]
for (const [price, allowance, persona] of upgradeRows) for (const s of S) {
  const incRev = price - 15
  const incFees = fees(price) - fees(15)
  const incCogs = Math.min(liveCogs(personas[persona], s) + baseChatCogs[s], 12) - Math.min(liveCogs(personas['Base — typical GM'], s) + baseChatCogs[s], 3)
  log(`| ${usd(price, 0)} (${allowance}) | ${s} | ${usd(incRev)} | ${usd(incFees)} | ${usd(incCogs)} | ${usd(incRev - incFees - incCogs)} |`)
}
log()
log('## Fixed and one-off costs (monthly amortization over 12 months)')
const fixedMonthly = { 'Memorystore Valkey custom-pico (realtime fanout, Phase 2+)': 22.48, 'Cloud SQL db-f1-micro → db-custom-1-3840 delta (shared with Workbench)': 49.31 - 7.67, 'Cloud KMS key version': 0.06, 'Cloud Scheduler (beyond free jobs)': 0.30 }
let fixedTotal = 0
for (const [k, v] of Object.entries(fixedMonthly)) { log(`- ${k}: ${usd(v)}`); fixedTotal += v }
log(`- **Recurring infrastructure total: ${usd(fixedTotal)} / month**`)
for (const oneOff of [10000, 25000, 50000]) {
  const monthly = oneOff / 12 + fixedTotal
  const perUpgrade = 25 - 15 - (fees(25) - fees(15)) - (liveCogs(personas['Premium — typical GM'], 'expected') + baseChatCogs.expected - Math.min(liveCogs(personas['Base — typical GM'], 'expected') + baseChatCogs.expected, 3))
  log(`- One-off legal + security review placeholder ${usd(oneOff, 0)} → ${usd(monthly, 0)}/month → **${Math.ceil(monthly / perUpgrade)} premium upgrades** at $25 (typical premium persona, expected) to break even within 12 months`)
}

console.log(lines.join('\n'))
