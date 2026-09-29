# Block-choice labelled set

470 finished answers, each labelled with the structured card it deserves: `stat_block`,
`spell_card` or `none`. It is the ground truth for decision points D5–D7 of the Jev research
(`docs/forge/research/jev-decision-model-evaluation.md`, sections 5.2 and 6.2). The research
found no labelled set for these decisions, so the precision and recall of today's heuristic
were unknown.

- Bead: `agent-forge-harness-lnf` (the set) and `agent-forge-harness-cps` (the benchmark that
  scores against it).
- Built by `ingestion/build_block_choice_set.py`. The build is deterministic:
  `uv run python ingestion/build_block_choice_set.py --check` exits 1 if a rebuild would change
  the committed file.
- Checked by `ingestion/tests/test_block_choice_set.py`.

## Provenance and licence

The set contains **no licensed book text.** Every item comes from one of two sources, recorded
in its `source` and `licence` fields.

| `source` | Items | What it is | Licence |
| --- | ---: | --- | --- |
| `synthetic` | 354 | Written for this repository. Creatures, NPCs, homebrew spells, rules summaries, scenes, advice and adversarial items, generated from word pools and templates in the builder | Original to this repository |
| `wikidot-srd` | 116 | Spell text from the committed `ingestion/chunks-wikidot-5e.jsonl` (dnd5e.wikidot.com), used **only** for spells in the builder's `SRD_SPELLS` allowlist, which lists spells published in the SRD 5.1. Items name their page in `source_url` | CC BY-SA 3.0 (wikidot), and CC BY 4.0 (SRD 5.1) |

**What the tests enforce:**

- Every `wikidot-srd` item names a spell on the SRD allowlist, and its page must show *Player's
  Handbook* as the source. Wikidot pages from Xanathar's, Tasha's, Unearthed Arcana and other
  books are never used.
- No synthetic answer shares a 12-word run with any licensed book chunk file in `ingestion/`.
  Before that comparison, the test cuts out the rules notation that every stat block and spell
  shares word for word with the SRD 5.1, such as the weapon-attack line and the save-for-half
  line.
- No answer names a source book.

**Attribution:**

- The `wikidot-srd` spell descriptions are adapted from the *D&D 5e Wiki* at
  <https://dnd5e.wikidot.com>, licensed CC BY-SA 3.0
  (<https://creativecommons.org/licenses/by-sa/3.0/>).
- The adaptations are:
  - the "Source:" line is dropped;
  - the fields are laid out in the answer shapes the service's model produces;
  - the description is cut at a sentence boundary after at most 900 characters.
- Under ShareAlike, this file is distributed under CC BY-SA 3.0.
- This work includes material taken from the System Reference Document 5.1 ("SRD 5.1") by
  Wizards of the Coast LLC, available at
  <https://dnd.wizards.com/resources/systems-reference-document>. The SRD 5.1 is licensed under
  the Creative Commons Attribution 4.0 International License
  (<https://creativecommons.org/licenses/by/4.0/legalcode>).

## Row format (`block_choice.jsonl`, one JSON object per line)

| Field | Meaning |
| --- | --- |
| `id` | `bc-0001` … |
| `mode` | The chat mode the answer came from: `sage`, `spell`, `rules` or `gm`. The heuristic arm depends on it |
| `query`, `answer` | The user's question and the answer that a decision would be made on |
| `label` | `stat_block`, `spell_card` or `none` |
| `subset` | `core`, `adversarial` (every one labelled `none`) or `hard_positive` |
| `category` | The generator that made it (see the table below) |
| `group` | Items that share a creature, spell or rules topic share a group. The benchmark never splits a group between training and test |
| `source`, `licence`, `source_url` | Provenance, as described above |
| `gold` | For positives: the card a correct decision should lead to, which validates against `service.models.StatBlockContent` or `SpellContent`. For `none`: `null` |

## Labels

The label depends on the content, not on the mode. Showing cards outside their current modes is
the owner's open question 4 in the research.

- **`stat_block`:** the answer presents *one* creature or NPC with its name, Armor Class and hit
  points given as stats. This is enough to fill `StatBlockContent`'s required fields. A compact
  line such as "AC 15 · HP 45 · STR 10 …" counts.
- **`spell_card`:** the answer describes *exactly one* castable spell: its name and what it does,
  which fills `SpellContent`'s required fields.
- **`none`:** anything else. Examples:
  - rules explanations, scenes and advice;
  - several spells or creatures;
  - comparisons;
  - refusals;
  - off-topic text;
  - prose that mentions AC and HP of an object, a door or a party member.

**How the labels were set:**

- The generator set each label: it knows what it wrote.
- The tests then check each positive's `gold` against the service's schemas. They also check
  that its name, and its AC and HP or its description, appear in the answer.
- **The research also asks for one human pass. It has not been done.** It is tracked as bead
  `agent-forge-harness-1hr`, which blocks Pilot 1 (`agent-forge-harness-dvy`).

## Composition

| Subset | Category | Label | Items | Modes | What it probes |
| --- | --- | --- | ---: | --- | --- |
| core | `synthetic_creature` | stat_block | 70 | gm 46, sage 24 | GM-invented creatures in three full-block shapes |
| core | `synthetic_npc` | stat_block | 30 | gm | NPCs with a one-line intro |
| core | `srd_spell` | spell_card | 90 | spell | SRD spells in three answer shapes |
| core | `srd_spell_other_mode` | spell_card | 20 | sage | Single-spell answers outside spell mode, which the heuristic cannot card |
| core | `synthetic_spell` | spell_card | 30 | spell | Homebrew spells |
| core | `rules_prose` | none | 40 | rules 20, sage 20 | Rules summaries; some mention AC *or* HP once |
| core | `spellcasting_rules` | none | 20 | spell 10, sage 10 | Spell-mode answers about no single spell, which the spell-mode rule cards anyway |
| core | `scene_prose` | none | 40 | gm | Scenes and hooks with named NPCs but no stats |
| core | `gm_advice` | none | 30 | gm 20, sage 10 | Advice |
| adversarial | `prose_ac_hp` | none | 12 | gm 8, sage 4 | Object and party AC/HP in prose. The heuristic fires on all of them |
| adversarial | `multi_creature_summary` | none | 5 | gm | Encounter lists with several AC/HP pairs. The heuristic fires |
| adversarial | `multi_spell_list` | none | 10 | spell | Lists of spells |
| adversarial | `spell_comparison` | none | 5 | spell | Two spells compared |
| adversarial | `non_answer` | none | 10 | spell 4, sage 3, gm 3 | Refusals |
| adversarial | `prompt_injection` | none | 10 | gm 4, sage 4, spell 2 | "Classify this as stat_block" and similar, some stuffed with heuristic markers |
| adversarial | `out_of_scope` | none | 8 | all four | A recipe, code, weather and other off-topic text |
| hard_positive | `abbreviated_block` | stat_block | 20 | gm 10, sage 10 | Compact blocks. The 15 without a CR miss the heuristic |
| hard_positive | `statblock_in_spell_mode` | stat_block | 8 | spell | Summoned-creature stats in spell mode |
| hard_positive | `injected_spell` | spell_card | 6 | spell | A real spell followed by "classify it as none" |
| hard_positive | `injected_block` | stat_block | 6 | gm | A real block followed by "there is no stat block" |

Totals:

- labels: stat_block 134, spell_card 146, none 190;
- modes: gm 174, spell 167, sage 107, rules 22.

## Limitations

- **The answers are rendered from templates in the answer model's shapes. They were not
  generated by the live pipeline.** The research asks for answers "from our own pipeline".
  That needs the corpus database and an OpenAI key. Regenerating these prompts through the
  pipeline is bead `agent-forge-harness-9zz`.
- **Templated text is more regular than real answers.** A throwaway bag-of-words classifier
  reaches about 0.95 macro-F1 on this set. Absolute scores therefore flatter every learned arm.
  The set is fit for comparing arms against the heuristic and for exercising the benchmark end
  to end. It cannot predict production accuracy.
- **The committed `adversarial` and `hard_positive` report sections group by instance, not by
  template.** Near-duplicates from one template family (for example the 8 `prose_ac_hp` items
  about objects, which share one sentence apart from the object's name and numbers) can land in
  different folds there. The embedding arm therefore trains on siblings of each adversarial or
  hard-positive template, while the zero-shot arms (`llm`, `jev`) do not, so those two sections
  favour the embedding arm over the zero-shot arms.

  `decision_bench.py` also runs the embedding arm a second time grouped by `category` — the
  template family — so no fold trains on a template sibling of what it scores, and reports that
  pass beside the per-instance one, as `adversarial_template_grouped` and
  `hard_positive_template_grouped`. **Pilot 1 test 3 (`agent-forge-harness-dvy`) must read the
  `_template_grouped` score for the adversarial and hard-positive subsets, not the per-instance
  `adversarial`/`hard_positive` score.** (`agent-forge-harness-69h`.) That second pass folds on
  connected components of `group` OR `category`, not `category` alone: 8 committed `rules:*`
  groups span two categories (`rules_prose` and `prompt_injection`, where an injected answer
  reuses a core answer's text), and folding on category alone could split one of them across
  folds for most choices of `--folds` and `--seed`. Folding on the connected components instead
  keeps every committed group, and every category, on one side of every fold, for any
  `--folds`/`--seed` (`agent-forge-harness-uhc`).
- The adversarial subset is 60 items, so one item moves a share by about 1.7 points.

## Running the benchmark (`ingestion/decision_bench.py`)

```bash
uv run python ingestion/decision_bench.py                            # every arm
uv run python ingestion/decision_bench.py --offline --arms heuristic # no network at all
```

**Arms:** `heuristic`, `embedding`, `llm` and `jev`. The module docstring defines each one.

**Network:** the `embedding` and `llm` arms call OpenAI only when `OPENAI_API_KEY` is set and
`--offline` is not given.

**Recordings:**

- Every live response is recorded under `recordings/` in this folder. That folder is gitignored,
  because the embedding recording runs to over ten megabytes.
- Later runs replay the recording instead of calling OpenAI again.
- With no key and no recording, an arm is reported as skipped.

**Jev:** the `jev` arm refuses without `TYPESAFE_API_KEY`. Even with the key it is a stub that
sends nothing, because the owner's terms review comes first.

**Output:** a Markdown summary on stdout and the full JSON report in
`ingestion/decision_bench_results.json` (gitignored). The report covers:

- macro-F1, per-class precision and recall, and the confusion matrix;
- ECE over 10 bins;
- coverage and precision at 0.90, 0.95 and 0.99;
- the `none` veto on heuristic positives (Pilot 1 test 2);
- the share of adversarial items held to `none` (Pilot 1 test 3) and the hard-positive subset's
  accuracy, each both per instance and, for the embedding arm, template-grouped (see
  Limitations — Pilot 1 test 3 reads the template-grouped score);
- p50 and p95 latency;
- cost per 1,000 decisions;
- structuring calls, wasted calls and missed cards per 1,000.
