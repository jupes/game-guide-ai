## Per listening hour (utterance uploads)
| Scenario | Billed audio min | STT | Extraction + cards + classifier + embeddings (classifier) | AI/STT overhead | Infra (compute, realtime, storage/KMS/logs, images) | **Total / listening hour** |
| --- | --- | --- | --- | --- | --- | --- |
| low | 37.8 | $0.063 | $0.041 ($0.012) | $0.005 | $0.008 | **$0.116** |
| expected | 43.7 | $0.197 | $0.073 ($0.019) | $0.027 | $0.025 | **$0.322** |
| stress | 50.4 | $0.282 | $0.569 ($0.101) | $0.255 | $0.118 | **$1.224** |

## Push-to-talk clip
| Scenario | Clip speech (s) | STT | Extraction + composition | **Total / clip** | 100 clips |
| --- | --- | --- | --- | --- | --- |
| low | 8 | $0.00023 | $0.00065 | **$0.0010** | $0.10 |
| expected | 12 | $0.00097 | $0.00097 | **$0.0022** | $0.22 |
| stress | 25 | $0.00261 | $0.01436 | **$0.0221** | $2.21 |

## Transcript upload (text) per transcript hour
| low $0.043 | expected $0.081 | stress $0.742 |

## 4-hour session, STT only, streaming (wall clock) vs utterance uploads
| Option | 4 h STT cost |
| --- | --- |
| Soniox stt-rt-v5 (diarization incl.) | $0.48 |
| AssemblyAI Universal-Streaming | $0.60 |
| AssemblyAI Universal-Streaming + diarization | $1.08 |
| ElevenLabs Scribe v2 Realtime | $1.56 |
| Deepgram Nova-3 streaming (regular) | $1.85 |
| AWS Transcribe streaming (diarization + vocab incl.) | $2.40 |
| Azure real-time | $4.00 |
| OpenAI gpt-live-transcribe | $4.08 |
| Utterance uploads (low) | $0.25 |
| Utterance uploads (expected) | $0.79 |
| Utterance uploads (stress) | $1.13 |

## Persona monthly live COGS (excludes base chat COGS)
| Persona | Clips | Listening h | Upload h | Low | Expected | Stress |
| --- | --- | --- | --- | --- | --- | --- |
| Base — light GM (PTT only) | 40 | 0 | 0 | $0.04 | $0.09 | $0.88 |
| Base — typical GM | 100 | 1 | 2 | $0.30 | $0.70 | $4.92 |
| Premium — typical GM | 150 | 8 | 2 | $1.16 | $3.07 | $14.59 |
| Premium $25 (12 h) — heavy GM at cap | 150 | 12 | 0 | $1.54 | $4.19 | $18.00 |
| Premium $25 (12 h) — power GM at cap | 400 | 12 | 4 | $1.96 | $5.06 | $26.50 |
| Premium $29 (20 h) — heavy weekly 4 h GM | 150 | 17.3 | 0 | $2.16 | $5.90 | $24.48 |
| Premium $29 (20 h) — power GM at cap | 400 | 20 | 4 | $2.89 | $7.64 | $36.28 |

## Plan contribution per subscriber (base chat COGS included; fees = 4.1% + $0.30)
| Plan / price | Persona | Scenario | Total COGS | Fees | Contribution | Margin |
| --- | --- | --- | --- | --- | --- | --- |
| Base $15 | Base — light GM (PTT only) | low | $1.04 | $0.92 | $13.05 | 87.0% |
| Base $15 | Base — light GM (PTT only) | expected | $1.59 | $0.92 | $12.50 | 83.3% |
| Base $15 | Base — light GM (PTT only) | stress | $3.00 (cap; raw $3.88) | $0.92 | $11.09 | 73.9% |
| Base $15 | Base — typical GM | low | $1.30 | $0.92 | $12.78 | 85.2% |
| Base $15 | Base — typical GM | expected | $2.20 | $0.92 | $11.88 | 79.2% |
| Base $15 | Base — typical GM | stress | $3.00 (cap; raw $7.92) | $0.92 | $11.09 | 73.9% |
| Premium $25 (12 h) | Premium — typical GM | low | $2.16 | $1.32 | $21.51 | 86.0% |
| Premium $25 (12 h) | Premium — typical GM | expected | $4.57 | $1.32 | $19.11 | 76.4% |
| Premium $25 (12 h) | Premium — typical GM | stress | $12.00 (cap; raw $17.59) | $1.32 | $11.68 | 46.7% |
| Premium $25 (12 h) | Premium $25 (12 h) — heavy GM at cap | low | $2.54 | $1.32 | $21.13 | 84.5% |
| Premium $25 (12 h) | Premium $25 (12 h) — heavy GM at cap | expected | $5.69 | $1.32 | $17.98 | 71.9% |
| Premium $25 (12 h) | Premium $25 (12 h) — heavy GM at cap | stress | $12.00 (cap; raw $21.00) | $1.32 | $11.68 | 46.7% |
| Premium $25 (12 h) | Premium $25 (12 h) — power GM at cap | low | $2.96 | $1.32 | $20.71 | 82.9% |
| Premium $25 (12 h) | Premium $25 (12 h) — power GM at cap | expected | $6.56 | $1.32 | $17.11 | 68.5% |
| Premium $25 (12 h) | Premium $25 (12 h) — power GM at cap | stress | $12.00 (cap; raw $29.50) | $1.32 | $11.68 | 46.7% |
| Premium $29 (20 h) | Premium $29 (20 h) — heavy weekly 4 h GM | low | $3.16 | $1.49 | $24.35 | 84.0% |
| Premium $29 (20 h) | Premium $29 (20 h) — heavy weekly 4 h GM | expected | $7.40 | $1.49 | $20.11 | 69.4% |
| Premium $29 (20 h) | Premium $29 (20 h) — heavy weekly 4 h GM | stress | $12.00 (cap; raw $27.48) | $1.49 | $15.51 | 53.5% |
| Premium $29 (20 h) | Premium $29 (20 h) — power GM at cap | low | $3.89 | $1.49 | $23.62 | 81.4% |
| Premium $29 (20 h) | Premium $29 (20 h) — power GM at cap | expected | $9.14 | $1.49 | $18.37 | 63.4% |
| Premium $29 (20 h) | Premium $29 (20 h) — power GM at cap | stress | $12.00 (cap; raw $39.28) | $1.49 | $15.51 | 53.5% |
| Premium $19 (12 h) | Premium $25 (12 h) — heavy GM at cap | low | $2.54 | $1.08 | $15.38 | 80.9% |
| Premium $19 (12 h) | Premium $25 (12 h) — heavy GM at cap | expected | $5.69 | $1.08 | $12.23 | 64.4% |
| Premium $19 (12 h) | Premium $25 (12 h) — heavy GM at cap | stress | $12.00 (cap; raw $21.00) | $1.08 | $5.92 | 31.2% |

## Portfolio (1,000 subscribers): 85% base, 15% premium at $25 with a 12 h allowance
| Scenario | Revenue | Fees | COGS | Contribution | Margin | Incremental live COGS |
| --- | --- | --- | --- | --- | --- | --- |
| low | $16500 | $977 | $1321 | $14202 | 86.1% | $321 |
| expected | $16500 | $977 | $2315 | $13208 | 80.1% | $815 |
| stress | $16500 | $977 | $4350 | $11174 | 67.7% | $1350 |

## Premium upgrade break-even (incremental over base $15, heavy persona at the plan allowance)
| Premium price (allowance) | Scenario | Incremental revenue | Incremental fees | Incremental COGS | Incremental contribution / upgrade |
| --- | --- | --- | --- | --- | --- |
| $19 (12 h) | low | $4.00 | $0.16 | $1.24 | $2.59 |
| $19 (12 h) | expected | $4.00 | $0.16 | $3.49 | $0.35 |
| $19 (12 h) | stress | $4.00 | $0.16 | $9.00 | $-5.16 |
| $25 (12 h) | low | $10.00 | $0.41 | $1.24 | $8.35 |
| $25 (12 h) | expected | $10.00 | $0.41 | $3.49 | $6.10 |
| $25 (12 h) | stress | $10.00 | $0.41 | $9.00 | $0.59 |
| $29 (20 h) | low | $14.00 | $0.57 | $1.86 | $11.57 |
| $29 (20 h) | expected | $14.00 | $0.57 | $5.19 | $8.23 |
| $29 (20 h) | stress | $14.00 | $0.57 | $9.00 | $4.43 |

## Fixed and one-off costs (monthly amortization over 12 months)
- Memorystore Valkey custom-pico (realtime fanout, Phase 2+): $22.48
- Cloud SQL db-f1-micro → db-custom-1-3840 delta (shared with Workbench): $41.64
- Cloud KMS key version: $0.06
- Cloud Scheduler (beyond free jobs): $0.30
- **Recurring infrastructure total: $64.48 / month**
- One-off legal + security review placeholder $10000 → $898/month → **125 premium upgrades** at $25 (typical premium persona, expected) to break even within 12 months
- One-off legal + security review placeholder $25000 → $2148/month → **298 premium upgrades** at $25 (typical premium persona, expected) to break even within 12 months
- One-off legal + security review placeholder $50000 → $4231/month → **586 premium upgrades** at $25 (typical premium persona, expected) to break even within 12 months
