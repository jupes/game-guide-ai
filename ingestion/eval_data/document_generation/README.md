# Document-generation eval cases

24 synthetic requests for the three document types a GM tool can generate (`npc`, `encounter`
and `session-notes`), eight per type. They are the offline fixture set for bead
`agent-forge-harness-1kg.5.4` (`service/document_generation.py`) and the input a live model
qualification run scores.

- Checked by `service/tests/test_document_generation_cases.py`: every row's shape, the coverage
  below, the licence, and that every case builds its messages offline within the context bound.
- **The runner, the scorecards, the thresholds and enabling any tool are `1kg.4.6`'s.** Nothing
  here calls a model.

## Provenance and licence

Every row is `"source": "synthetic"`: written for this repository, with no licensed book text.
Every corpus passage names the book `synthetic-5e` and was written for this file. The test
checks that no case shares a 12-word run with any `ingestion/chunks*.jsonl` book text.

## Coverage

Per type: at least three `representative` cases, at least one `edge` case (a minimal brief and a
brief of exactly 2,000 code points), and at least two `injection` cases. Across the file every
injection family appears at least once:

| Family | What the hostile text tries |
| --- | --- |
| `instruction_override` | Replace the rules ("ignore all previous instructions") |
| `delimiter_spoof` | Close the data block early with a forged tag |
| `exfiltration_link` | Plant a remote image or link in a field |
| `field_hijack` | Set a server-owned key (`portrait`, `tags`) |
| `citation_fabrication` | Cite passage numbers that were never supplied |
| `json_breakout` | Break out of a JSON string into the envelope |
| `model_identity` | Make the output name the model or its company |
| `type_switch` | Turn the requested type into another |

The hostile text sits in the brief, a thread turn, the campaign tone, the source result or a
corpus passage.

## Row format (`cases.jsonl`, one JSON object per line)

| Field | Meaning |
| --- | --- |
| `id` | `dg-<type>-NNN` |
| `doc_type` | `npc`, `encounter` or `session-notes` |
| `category` | `representative`, `edge` or `injection` |
| `injection_family` | One of the families above for an `injection` case, else `null` |
| `source`, `licence` | Always `synthetic`, and the fixed licence line |
| `request` | `brief`, `campaign` (`name`, `tone`), `thread` (`speaker`, `text`), `source_result`, `corpus` (`text`, `source`) and `preset` — the fields of `GenerationRequest` |
| `expect.basis` | The bases a correct output may earn (`invented`, `mixed`, `thread`) |
| `expect.must_fill` | Keys that must be filled (at least the type's own `must_fill`) |
| `expect.forbid_substrings` | Text no stored value may contain (every remote-reference marker, plus the payload a hostile case plants) |
| `expect.max_cited` | The most passages an output may cite |
| `expect.must_mention` | Usefulness: facts from the brief or thread the output must carry |
| `expect.forbid_facts` | Hallucination: events or facts that appear nowhere in the input |
