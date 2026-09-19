# Workbench wire contract — fixtures, v1

These files are the **specification** of the Workbench wire contract. Two test
suites read them, one per language, so the Pydantic models and the Zod schemas
cannot drift apart:

- `service/tests/test_workbench_contracts.py`
- `ui/src/gm/contracts.test.ts`

Conventions, versioning rules and the status of each schema family are in
[`docs/workbench-wire-contract.md`](../../../docs/workbench-wire-contract.md).

## Files

| File | Purpose |
| --- | --- |
| `schemas.json` | Every schema name. A suite fails if it lacks one, or if one has no fixture. |
| `registry.json` | The registry facts the validators rely on. Both languages keep them as constants and both suites compare them with this file. `1kg.3.1` extends it. |
| `<Schema>.json` | Examples for one schema. The file is named after the schema. |
| `legacy/` | Guards, not new contracts: today's `/chat` and message-history responses, validated by the existing models. |
| `../tools/differential_fuzz.py` | Mutates every valid example here and compares the two validators. Run it after changing a schema; CI runs it as `contract-parity`. |

## Fixture format

```json
{
  "schema": "ToolSuggestion",
  "direction": "response",
  "valid":   [{ "name": "minimal", "value": { "tool_id": "portrait", "label": "Portrait" } }],
  "invalid": [{ "name": "an empty label", "why": "bounded 1..60", "value": { "tool_id": "npc", "label": "" } }]
}
```

- Every invalid example says **why**, citing a decision ID where one applies.
- `"applies_to": ["server"]` or `["client"]` limits an example to one side. Use it
  only for the deliberate asymmetry — a strict server, a client that tolerates
  additive fields and unknown error codes — never to hide a disagreement.
- The string `"@repeat:a:2000"` expands to 2,000 `a`s in both suites, so boundary
  cases stay readable. The repeated unit is one code point: `"@repeat:🎲:2000"`
  is 2,000 characters on both sides.
- The object `{ "@repeat_value": <anything>, "@count": 101 }` expands to a list of
  101 copies, for bounds on lists.
- `"direction"` is `request`, `response` or `both`. It documents; it does not
  change how an example is checked.
