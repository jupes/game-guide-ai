# The canary leak-test harness

Test infrastructure for every security test that asks "did private text reach a place it must never
be?" (bead `agent-forge-harness-1ir.1.10`). A test mints synthetic secrets, called **canaries**, into
every private place, routes the code under test through recording fakes and captures, and calls
`assert_clean`. The harness lives in `service/tests/canary/` and is never product code.

## 1. What it is for

It is the instrument behind the Live Session Assistant plan's test families 1 (canary seeding),
2 (capture everything), 3 (canary assertions on player paths, including failures) and 16 (log, trace
and metric hygiene) in `docs/forge/plans/live-session-assistant.md` section 4.11. The families
themselves belong to `1ir.13.6` and `1ir.6.3`; this harness makes them cheap to write.

| Threat-model rows | What the harness gives | Proven in |
| --- | --- | --- |
| TM-01, WT-1, SEC-14, SEC-15, T-1, AE-27: a private field reaches a player | a distinct canary in every text slot of every document type; the `PLAYER` policy; raw HTTP bytes and every header; frames as bytes | `test_canary_harness.py`, `test_canary_demo.py` |
| TM-02, WT-2: leakage across campaigns or recipients | identical-name twin campaigns; the `GM` campaign check; unregistered tokens; key-like cache keys | `test_canary_harness.py` |
| TM-07, SEC-28, AUDIO-29: metadata, filenames, titles | title, filename, alt-text, cue and source surfaces; header scanning | `test_canary_harness.py`, `test_canary_sinks.py` |
| TM-08, WT-11, SEC-20 to SEC-24, T-12, family 16 | every log record, stdout, warnings, metric labels, trace callbacks, the Langfuse client, audit and outbox rows | `test_canary_sinks.py`, `test_canary_demo.py` |
| ED-26: no digest of field text | digest needles, hex and base64 | `test_canary_harness.py` |
| D-12, T-24: a held copy reaches no stream | `expect_empty` | `test_canary_harness.py` |

It asserts on the raw bytes a sink is handed, never on a parsed result, as the wire contract's canary
rule requires.

## 2. Quick start

```python
from service.tests.canary import Audience, CanaryLeak, CanaryWorld, LeakCapture


def test_a_player_card_shows_only_the_mask(leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    npc = canary_world.document("npc")                        # a valid NPC, a canary in every text slot
    llm = leak_capture.llm("card-llm", audience=Audience.PLAYER, replies=["a card"])
    slot = leak_capture.channel("table-slot", audience=Audience.PLAYER)

    card = my_card_composer(npc.data, mask=["name", "voice"], llm=llm)   # the code under test
    slot.publish(card.encode(), topic="slot-table")

    shown = npc.field("name") | npc.field("voice")
    leak_capture.assert_clean(
        visible={"card-llm": shown, "table-slot": shown},     # what each player sink may carry
        must_see={"card-llm": npc.field("name")},             # proof the path really ran
    )
```

`leak_capture` and `canary_world` are pytest fixtures, registered for every test directory by one
line in the repository's root `conftest.py`. Request `leak_capture` first among a test's fixtures.

## 3. Audiences and key-like facets

Every sink declares an audience. The policy is a pure function of the captures and these rules.

| Where the canary was captured | A registered canary | A canary-shaped token nobody minted |
| --- | --- | --- |
| A `TELEMETRY` sink: logs, stdout and stderr, warnings, metrics, traces, rows | always a `LEAK`, even a "public" one (SEC-20) | `UNREGISTERED` |
| A key-like facet of any sink: `key`, `topic`, `recipient`, `url`, `config`, and the headers `location`, `content-location`, `link`, `refresh` and `set-cookie` | always a `LEAK`: keys, topics and URLs end up in logs and dashboards | `UNREGISTERED` |
| A `PLAYER` sink, other facets | a `LEAK` unless listed in `visible` for that sink | `UNREGISTERED` |
| A `GM` sink declared with `campaign=` | `FOREIGN_CAMPAIGN` unless the canary belongs to that campaign | `UNREGISTERED` |
| A `GM` sink without a campaign | allowed | allowed |

- `PLAYER` covers every table principal: a seated account, the owner's player view (T-23) and a
  screen grant (SEC-48, D-13). Never model those as `GM`: a `GM` sink without a campaign is unchecked.
- The internal bus (NOTIFY) and the outbox are `TELEMETRY` (SEC-20).
- The audience is a required keyword on every sink factory except the telemetry ones, so a player
  sink cannot become an unchecked one by omission.

## 4. The canary world

- **Tokens.** `cnry` and 16 lowercase base32 characters (`TOKEN_PATTERN`), derived from the test's
  node id and a counter: a failure reproduces with the same tokens. A second world on a seed already
  used in the process is a `HarnessMisuse`, because two would mint the same tokens.
- **Values.** `f"{token} {filler} {token}"`: lowercase ASCII words, one line, at most 100 characters,
  so the token survives truncation from either end and any normalising digest. Some surfaces have
  the shape the product accepts: `ALIAS` and `GROUP_NAME` are the bare token (the product bounds them
  at 40 characters), `EMAIL` is `token@token.example`, `URL` is `https://token.example/token`, and
  `FILENAME` is `token-words-token.png`. `mint(..., max_chars=n)` fits any other bound.
- **Documents.** `world.document("npc")` builds a whole document that `check_fields` accepts, with a
  distinct canary in every text slot: text and prose fields, two items per list, and a name and a
  text for each of two entries. Integers take their lowest valid value and ability blocks are left
  out. An asset field carries alt text in the data (a canary) plus a `FILENAME` sidecar canary, because
  a stored asset reference has no filename slot. `doc.field(key)` returns what a field carries;
  `doc.sidecar(key, Surface.FILENAME)` returns the sidecar. `world.every_type()` builds one of each.
- **Twins.** `world.twin("A", as_campaign="B")` mirrors every document of A into B with fresh tokens.
  With `identical_names=True` (the default) each `name` is A's own canary, byte for byte, and
  `world.campaigns_of(token)` returns both campaigns. The dangerous cross-campaign leak is B's other
  fields reaching A, and those are distinguishable.
- **Fixture types.** `world.fields({"key": FieldKind.TEXT, ...})` fills an arbitrary mapping the same
  way, for fixture types outside the registry.
- **Visibility.** `visible` comes from the test's explicit mask or from the policy oracle, never from
  product code. The harness imports no policy, projection, registry or oracle module, so it cannot
  agree with their bugs.

## 5. The sinks, and how to wire each into code under test

| Factory | Records | Wire it through |
| --- | --- | --- |
| `llm(label, audience=...)` | `messages` (every message as JSON: role, content, and every other field of its `model_dump()`, such as tool calls and tool call ids), `config` (every key; key-like), `kwargs` | `RagService(llm_client=...)` or `ProviderClientFactory(client_builders={alias: llm})`; `invoke` and `ainvoke` |
| `embeddings(label, audience=...)` | every `input` string, `model`, any other `kwargs` | `embed_query(text, client=...)` |
| `stt(label, audience=...)` | `audio` bytes, `glossary`, `options` | provisional; see section 12 |
| `cache(label, audience=...)` | every `key` (key-like), every stored `value` | provisional |
| `channel(label, audience=...)` | each `frame` as the bytes sent, `topic`, `recipient` (key-like) | provisional; one sink per recipient |
| `metrics()` / `install_metrics(app)` | each point's JSON | `app.state.metrics_sink`; exit restores the previous value or removes it |
| `tracing()` | every LangChain callback event, and every Langfuse client call | sets `RAG_TRACING=1` and replaces `langfuse.langchain.CallbackHandler` and `langfuse.get_client` through `monkeypatch` |
| `http(label, response, audience=...)` | the raw body bytes, every header (duplicates included), the request URL (key-like) | any `httpx.Response`, such as a `TestClient` response |
| `rows(label, rows)` | one JSON capture per row, dataclasses as their fields | audit rows and outbox payloads |
| `record(label, kind=..., audience=..., facet=..., data=...)` | exactly `data` | anything else (section 9) |

The log capture needs no wiring. While a capture is active it holds a handler on the root logger and
on every logger that exists with `propagate=False`, lowers the root to DEBUG, lifts
`logging.disable`, formats each record when it is emitted (message, arguments, `extra=` attributes,
the whole exception chain with causes, contexts, notes and each exception's `repr`, and the stack),
captures stdout and stderr, and captures warnings, re-issuing them after exit so pytest still reports
them. Every change is undone at exit, in reverse order.

## 6. `assert_clean`, the vacuity guards and the report

`assert_clean(visible=..., must_see=..., expect_empty=...)` probes, scans everything captured so far,
and raises `CanaryLeak` with every finding. It may be called more than once.

- `visible[sink]`: the canaries a `PLAYER` sink may carry. Nothing is visible unless declared.
- `must_see[sink]`: every listed canary must appear (the positive control). Missing ones are `MISSING`.
- `expect_empty`: sinks that must record nothing, such as a held reveal that must reach no stream
  (D-12). A listed sink with traffic is `NOT_EMPTY`; an unlisted `PLAYER` sink with none is `VACUOUS`.
- **Probes.** Each `assert_clean` sends a DEBUG log record, a warning, and a line to stdout and to
  stderr. One that does not arrive is `PROBE_LOST`: a handler was removed, a level raised, logging
  disabled, `sys.stdout` replaced, or a `catch_warnings` opened. A replaced `app.state.metrics_sink`
  is reported the same way.
- **Unasserted.** A test whose call phase passed without `assert_clean` errors at teardown with
  `CaptureNotAsserted`.
- **Late captures.** What is captured after the last `assert_clean` (other fixtures' teardown, late
  threads) is scanned at exit under that call's `visible` and `expect_empty`.
- **The report** lists each finding on one line: category, sink, audience and kind, record index,
  facet, needle, canary label and token, a count, and an excerpt of at most 80 characters with the
  hit in `[[...]]`. For `authorization`, `cookie`, `set-cookie` and `proxy-authorization` headers
  the excerpt shows only the hit. `HarnessMisuse` messages never print a canary.

## 7. What is detected, and what is not

Detected, per canary: the token in any case; the token base64-encoded at any byte offset inside
larger data (such as a cursor); the token's hex; and the md5, sha1, sha256 and blake2b-256 digests of
the token and of the whole value, as hex in any case and as standard or URL-safe base64, padded or
not (ED-26). Bytes are scanned as they are. Every canary-shaped token nobody in the world minted is
reported too.

Not detected, by design: HMACs; compressed or encrypted data; a token with spaces or zero-width
characters inserted; base64 wrapped across lines; an upper-cased token that was then base64-encoded;
UTF-16; and fragments of a value that do not include a token. Detecting those is the egress filter's
job (`1ir.11.5`).

## 8. Rules

- **Never feed real data to the harness.** Captures hold whatever the code under test logs and sends.
- **Never import it from production code.** `test_canary_sinks.py` fails if any module under
  `service/` outside `service/tests/` imports `service.tests`. The production image contains
  `service/tests/**`, so that test is the control. Import the harness only as `service.tests.canary`.
- **Every canary test proves its canary was accepted**, by a status code or by `must_see` on a GM
  sink. A canary the product refused (a 422) proves nothing.
- **One sink per recipient.** A channel sink records frames for one principal.
- **Request `leak_capture` first**, so it captures what other fixtures log while they are set up.
- **A URL carrying a canary is flagged by design**, including `httpx`'s own request log line: a URL
  must never carry private text (SEC-20).
- `leak_capture` reads stdout and stderr through pytest's `capteesys`, because pytest swaps
  `sys.stdout` between test phases. So it cannot be combined with `capsys` or `capfd` (pytest refuses),
  or with a test's own `capteesys.readouterr()` (which would consume the output first). A
  conftest-level fixture that needs stdio uses `leak_capture`, not a bare `LogCapture`.
- Loggers created after the capture starts with `propagate=False` are not captured. A level set on a
  logger is honoured, as in production (`psycopg.pool` at ERROR). Importing `langfuse` sets the
  `httpx` and `langfuse` loggers to WARNING, and gives `httpx` a console handler, for the rest of the
  process; after that, `httpx`'s INFO request lines are not emitted at all. A suite-wide sweep that
  needs them resets that logger first. The log probe proves only the root path: a logger with its own
  level above DEBUG, or with `disabled=True`, drops records without a `PROBE_LOST`. Probe records appear in `caplog.records` inside a
  `leak_capture` test.
- Inside a capture every warning is recorded, so a test relying on `-W error` loses it there.
- Captures in concurrent threads of one process are unsupported; nested captures exit in LIFO order.
- `tracing()` models only SEC-24's *absent* option: it sees everything a Langfuse handler would be
  handed, before any masking. It is not evidence for or against a masking handler; `1ir.6.3` owns
  that check at the exporter.
- Never write the literals for a path insertion into `sys.path` or the database marker in any new
  file under `service/`, prose included: the repository's text scans catch them.

## 9. Adding a sink kind

Wrap the new seam in a small fake that records at the moment it is called, before it returns or
raises, with `LeakCapture.record(label, kind=SinkKind.CUSTOM, audience=..., facet=..., data=...)`.
Record the raw `str` or `bytes` handed over, never a parsed object. Name key-like data with a facet in
`KEY_LIKE_FACETS`. Then prove the fake with a mutant: remove the `record` call and watch a test fail.
`1ir.13.6` records SQL parameters this way.

## 10. Watching the demonstration fail

```bash
uv run --frozen --no-sync python -m pytest -q service/tests/canary_demo_flow.py
```

`test_demonstration[none]` passes; `[player_capture]` and `[log]` fail with `CanaryLeak`, naming the
sink, the field (`true_identity`) and the excerpt; `test_forgot_to_assert` errors at teardown with
`CaptureNotAsserted`. The module is not collected by a normal run; `test_canary_demo.py` runs it in a
subprocess and asserts each outcome from its JUnit report.

## 11. Suite-wide use (`1ir.6.3`)

```python
from service.tests.canary import LogCapture, sweep_for_canaries

with LogCapture() as logs:
    ...  # run the live suite's paths
findings = sweep_for_canaries(logs.captures())
```

`sweep_for_canaries` needs no world: it reports every canary-shaped token. Exclude the harness's own
test modules, which emit canaries on purpose: `test_canary_harness.py`, `test_canary_sinks.py` and
`test_canary_demo.py`. Inside a pytest test, prefer `leak_capture`, which also sees stdout.

## 12. Who owns the provisional fakes

The product interfaces for these do not exist yet. Each fake records every argument; the bead that
builds the real interface adapts its fake and keeps these recording semantics. `RecordingSTT`'s
`glossary` is a sequence of terms: one string (which type-checks as `Sequence[str]`) raises
`HarnessMisuse` rather than being recorded as its characters, until `1ir.4.3` settles the real type.

| Fake | Owner of the real interface |
| --- | --- |
| `RecordingSTT` | `1ir.4.3` |
| `RecordingCache` | `1ir.2.5` |
| `RecordingChannel` | `1ir.11.2` and `1kg.7.2` |
