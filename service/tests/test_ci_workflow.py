"""Repository-level contract for PR E2E gating and deploy safety."""

import ast
import re
import tomllib
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")

#: The runner image every job pins. `ubuntu-latest` becomes Ubuntu 26 on
#: 2026-10-19, and this file is also the master deploy workflow, so the image
#: changes only when someone changes it here, after verifying every job on the
#: new one: PostgreSQL service, Playwright, and the deploy job's gcloud steps
#: (agent-forge-harness-7q6).
RUNNER = "ubuntu-24.04"

#: The first major of each action whose action.yml says `runs.using: node24`.
#: Node.js 20 actions are deprecated on GitHub-hosted runners. An action missing
#: from this table fails the test until someone checks its runtime and adds it.
#: setup-uv publishes no major tags after v7 (v8.0.0 onwards are immutable exact
#: versions), so its major compares the same whichever form is used.
FIRST_NODE24_MAJOR = {
    "actions/checkout": 5,
    "actions/upload-artifact": 6,
    "astral-sh/setup-uv": 7,
    "oven-sh/setup-bun": 2,  # the floating v2 tag resolves to a node24 release (v2.2.0)
    "google-github-actions/auth": 3,
    "google-github-actions/setup-gcloud": 3,
}

#: Tests that only mean something against a real database. Each must be reachable
#: from CI with DATABASE_URL set, or it silently reverts to a permanent skip.
DB_BACKED_TESTS = [
    "service/tests/test_invite_atomic.py",
    "tests/test_schema.py",
    "tests/test_migrations_db.py",
    "tests/test_db_postgres.py",
    "tests/test_campaign_db.py",
    "tests/test_campaign_summary_db.py",
    "tests/test_conversation_db.py",
    "tests/test_seats_db.py",
    "tests/test_timeline_db.py",
    "tests/test_tool_invocation_db.py",
    "tests/test_document_db.py",
    "tests/test_documents_api_db.py",
    "tests/test_usage_ledger_db.py",
    "tests/test_retired_model_binding_db.py",
    "tests/test_corpus_schema.py",
    "ingestion/tests/test_scrape_wikidot.py",
    "tests/test_asset_db.py",
    "tests/test_session_dividers_db.py",
    "tests/test_reveal_db.py",
    "tests/test_eligibility_db.py",
]


def _python_job() -> str:
    return WORKFLOW.read_text(encoding="utf-8").split("\n  python-tests:\n", 1)[1].split(
        "\n  ui-tests:\n", 1
    )[0]


def _jobs() -> dict[str, str]:
    """Each job's name mapped to its block, read from the workflow's `jobs:` key."""
    body = WORKFLOW.read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    parts = re.split(r"^ {2}([A-Za-z0-9_-]+):[ \t]*\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _integration_step_run() -> str:
    """The `run:` block of the PostgreSQL step: the files pytest is actually given.

    Read from the command, not searched for in the job's text, because the
    comments around the step can name a file the command no longer runs.
    """
    step = _python_job().split("- name: Integration tests against real PostgreSQL\n", 1)[1]
    run = re.search(r"^ {8}run: \|\n((?: {10}.*\n?)+)", step, re.M)
    assert run, "the integration step must keep its multi-line `run: |` pytest command"
    return run.group(1)


def _imports_pg_module(text: str) -> bool:
    """True if `text` imports `_pg` or `tests._pg`, in any form Python accepts.

    Minting `DSN`, `needs_db`, `throwaway_database` or `corpus_database` from
    `_pg`/`tests._pg` is the gate a module takes on a real database, so the
    import line alone is enough -- without guessing at which of those names it
    uses (a bare `DSN` would also match an unrelated same-named local, e.g.
    `tests/test_bootstrap_db.py`'s fake one).

    AST-based, not a column-0-anchored regex: `^from\\s+(?:tests\\.)?_pg\\s+
    import\\b` missed `import tests._pg as pg`, `from tests import _pg`, and
    an import indented inside a `try:`/`if:` block, all of which are legal
    Python imports that `ast.walk` finds regardless of indentation or import
    style (agent-forge-harness-opn / #137 M-1).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in ("_pg", "tests._pg") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module in ("_pg", "tests._pg"):
                return True
            if node.module == "tests" and any(alias.name == "_pg" for alias in node.names):
                return True
    return False


def _database_backed_test_files() -> list[str]:
    """Every test module under pytest's testpaths that gates on a real database.

    A module is database-backed when it uses the `needs_db` marker, reads
    DATABASE_URL itself, or imports from `_pg`/`tests._pg` at all (see
    `_imports_pg_module`). Discovered, not listed, so the next one cannot be
    added without CI running it: the seven tests from #53 skipped on every run
    because nobody added them to a list (agent-forge-harness-5fo), and a
    module using `tests._pg`'s own gate rather than `needs_db`/DATABASE_URL
    directly in its own text was the same gap again (#124 M-2).
    """
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    reads_dsn = re.compile(
        r"""(?:environ\.get|getenv)\(\s*["']DATABASE_URL["']|environ\[\s*["']DATABASE_URL["']\s*\]""",
    )
    this_file = Path(__file__).resolve()
    found: list[str] = []
    for root in testpaths:
        for path in sorted(Path(root).rglob("test_*.py")):
            if path.resolve() == this_file:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bneeds_db\b", text) or reads_dsn.search(text) or _imports_pg_module(text):
                found.append(path.as_posix())
    return found


def _deploy_gates() -> list[str]:
    """The clauses the deploy job's job-level `if:` ANDs together at its top level.

    Read out of the `if: >-` block itself, not searched for in the job's text: the
    comments around it quote the same expressions, and a guard that survives only
    in a comment guards nothing. Splitting on the `&&`s outside every parenthesis
    is what makes a clause a gate — one demoted into the `||` group would still be
    "in" the condition and no longer be required by it.
    """
    deploy_job = WORKFLOW.read_text(encoding="utf-8").split("\n  deploy:\n", 1)[1]
    block = re.search(r"^ {4}if: >-\n((?: {6}.*\n)+)", deploy_job, re.M)
    assert block, "the deploy job must keep its job-level `if: >-` block"
    condition = " ".join(block.group(1).split())

    gates: list[str] = []
    depth = start = 0
    for index, char in enumerate(condition):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and condition.startswith("&&", index):
            gates.append(condition[start:index].strip())
            start = index + 2
    gates.append(condition[start:].strip())
    return gates


def test_ci_runs_e2e_on_pull_requests_and_never_deploys_them():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "\n  pull_request:\n" in workflow
    assert "\n  ui-e2e:\n" in workflow
    e2e_job = workflow.split("\n  ui-e2e:\n", 1)[1].split("\n  deploy:\n", 1)[0]
    assert "bun run test:e2e" in e2e_job
    assert "actions/upload-artifact@v7" in e2e_job
    assert "ui/e2e-results" in e2e_job

    deploy_job = workflow.split("\n  deploy:\n", 1)[1]
    assert "ui-e2e" in deploy_job.split("\n    if:", 1)[0]
    assert "github.event_name != 'pull_request'" in deploy_job


def test_ci_deploys_only_from_master():
    """`workflow_dispatch` takes any ref, so `gh workflow run ci.yml --ref <branch>`
    reaches the deploy job with that branch's SHA. The WIF trust condition
    (docs/deploy-gcp.md §8) would still refuse the token, but as a red deploy job
    failing at authentication — and as the only thing in the way."""
    gates = _deploy_gates()

    assert "github.ref == 'refs/heads/master'" in gates, (
        "the deploy job must be gated on the master ref at the top level of its "
        f"`if:` — a run on any other ref tests without deploying. Gates found: {gates}"
    )
    # The guard is one more gate, not a replacement for the ones already there.
    assert "github.event_name != 'pull_request'" in gates
    assert "vars.DEPLOY_TARGET != ''" in gates


# ── The database-backed tests must actually RUN in CI ────────────────────────
#
# The failure these prevent is invisible: a skipped test reports the same green
# tick as a passing one, so the atomic invite guarantee could go unverified on
# every run with nothing to show for it.


def test_ci_provides_a_postgres_service_for_the_python_job():
    job = _python_job()
    assert re.search(r"^\s{4}services:$", job, re.M), (
        "python-tests must declare a `services:` block — without a database the "
        "integration tests skip, and a skip looks exactly like a pass"
    )
    assert re.search(r"image:\s*(?:postgres|pgvector/pgvector):", job), (
        "the service must be a postgres image"
    )
    assert "--health-cmd" in job, (
        "the postgres service needs a health check, or the test step races the "
        "database's startup and fails for a reason that has nothing to do with the code"
    )


def test_ci_postgres_is_the_image_the_stack_runs():
    """The corpus schema tests apply vector-db/init/, whose first statement is
    `CREATE EXTENSION vector`. A stock postgres image has no pgvector, so the CI
    database has to be the one docker-compose.yml runs, major version included."""
    job_image = re.search(r"^ {8}image:\s*(\S+)\s*$", _python_job(), re.M)
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    stack_image = re.search(r"^ {2}vector-db:\n(?: {4}.*\n)*? {4}image:\s*(\S+)\s*$", compose, re.M)
    assert job_image and stack_image, "both the CI service and compose's vector-db must name an image"
    assert job_image.group(1) == stack_image.group(1), (
        f"CI's PostgreSQL is {job_image.group(1)} but the stack runs {stack_image.group(1)}; "
        "the corpus schema needs the pgvector extension the stack's image provides"
    )


def test_ci_runs_the_database_backed_tests_with_a_dsn():
    job = _python_job()
    assert "DATABASE_URL:" in job, (
        "CI must set DATABASE_URL for the integration step; without it "
        f"{DB_BACKED_TESTS} skip themselves and verify nothing"
    )
    for test in DB_BACKED_TESTS:
        assert test in job, f"CI must invoke {test} (it is skip-only without a DSN)"


def test_every_database_backed_test_file_runs_in_the_integration_step():
    """Self-maintaining: a module that gates on DATABASE_URL skips everywhere
    but the integration step, so it must be named in that step's command. The
    list above is checked against the same discovery, so it cannot fall behind
    either (agent-forge-harness-5fo)."""
    discovered = _database_backed_test_files()
    assert set(DB_BACKED_TESTS) <= set(discovered), (
        "discovery must recognise every known database-backed test, or it is not "
        f"looking: missed {sorted(set(DB_BACKED_TESTS) - set(discovered))}"
    )
    named = set(re.findall(r"[\w./-]+\.py", _integration_step_run()))
    not_run = [path for path in discovered if path not in named]
    assert not not_run, (
        f"{not_run} gate on DATABASE_URL but the integration step does not run them, "
        "so they skip on every CI run and a skip looks exactly like a pass"
    )
    unlisted = [path for path in discovered if path not in DB_BACKED_TESTS]
    assert not unlisted, f"add {unlisted} to DB_BACKED_TESTS"


def test_pg_import_detection_matches_every_legal_import_form():
    """The old column-0 regex only caught `from _pg import ...` / `from
    tests._pg import ...`. These forms are equally real Python and equally
    mint a database gate from the same module -- an AST scan must catch them
    all (agent-forge-harness-opn / #137 M-1)."""
    matches = [
        "from _pg import connect, needs_db\n",
        "from tests._pg import corpus_database, needs_db\n",
        "import tests._pg as pg\n",
        "import _pg\n",
        "from tests import _pg\n",
        "try:\n    from tests._pg import needs_db\nexcept ImportError:\n    pass\n",
        "if True:\n    from _pg import needs_db\n",
    ]
    for text in matches:
        assert _imports_pg_module(text), f"missed a real _pg import: {text!r}"

    non_matches = [
        "from typing import Any\n",
        "import pg8000\n",  # a same-prefix, unrelated package must not match
        "# from tests._pg import needs_db -- just a comment\n",
    ]
    for text in non_matches:
        assert not _imports_pg_module(text), f"matched something that is not a _pg import: {text!r}"


def test_the_ast_import_scan_actually_changes_discovery(tmp_path, monkeypatch):
    """`_database_backed_test_files()` ORs three signals together: `needs_db`,
    a `DATABASE_URL` read, or `_imports_pg_module`. Every file already in
    `DB_BACKED_TESTS` also matches one of the first two, so dropping the
    `_imports_pg_module` clause changes nothing there -- a mutant that deletes
    it still passes the whole suite. This proves the clause is load-bearing,
    against a fixture module that matches ONLY through the AST scan: no
    `needs_db` marker, no DATABASE_URL read, just `import tests._pg as pg` --
    one of the forms `test_pg_import_detection_matches_every_legal_import_form`
    proves `_imports_pg_module` recognises but the older column-0 regex missed
    (agent-forge-harness-8ug / #147 M-1)."""
    testdir = tmp_path / "tests"
    testdir.mkdir()
    (testdir / "test_ast_only.py").write_text("import tests._pg as pg\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    discovered = _database_backed_test_files()

    assert "tests/test_ast_only.py" in discovered, (
        "a module that imports tests._pg only in a form the needs_db/DATABASE_URL "
        "regexes miss must still be discovered through the AST scan -- dropping "
        "the `_imports_pg_module` clause from `_database_backed_test_files` would "
        "silently stop this and no other test would notice"
    )


def test_the_dsn_is_scoped_to_the_integration_step_not_the_whole_job():
    """A job-wide DATABASE_URL would change the app's startup path in every
    unrelated test (the lifespan builds a real auth store when it can connect),
    so the variable belongs to the one step that wants it."""
    job = _python_job()
    dsn_index = job.index("DATABASE_URL:")
    # The `env:` that owns it must sit inside a step, i.e. after the job's
    # `steps:` key — not in a job-level `env:` block above it.
    assert "\n    steps:" in job and job.index("\n    steps:") < dsn_index, (
        "DATABASE_URL must be set on a step, not on the whole python-tests job"
    )


def test_the_integration_step_does_not_swallow_its_own_failure():
    job = _python_job()
    step = job.split("Integration tests against real PostgreSQL", 1)[1]
    assert "continue-on-error" not in step, (
        "the integration step must be able to fail the job — allowing it to "
        "continue would make the job green whatever the database did"
    )


# ── A test that cannot run is as bad as one that is never invoked ────────────


def test_no_database_backed_module_defines_a_top_level_name_twice():
    """Top-level `def` and `class` only — a name bound twice inside a function
    is a different question and not this one.

    A second `def` of the same name silently replaces the first, and every
    caller written against the first signature then fails — but only when the
    test actually RUNS. For the files in `DB_BACKED_TESTS` that is CI, on a
    branch, after a push.

    ruff's F811 does not catch it when the first binding is used before the
    redefinition, which is exactly the shape that got here: one
    `_a_participant(dsn)` helper and one `_a_participant(world, ...)`, in one
    module, with the CI-only test calling what it thought was the first.
    """
    import ast

    for path in (Path(name) for name in DB_BACKED_TESTS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen: dict[str, int] = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if node.name in seen:
                raise AssertionError(
                    f"{path}: '{node.name}' is defined at line {seen[node.name]} and again at "
                    f"line {node.lineno}; the second wins and the first is unreachable"
                )
            seen[node.name] = node.lineno


def test_contract_parity_gates_deploy():
    """agent-forge-harness-oe6: the timeline route serves the Workbench contract,
    so a Pydantic/Zod disagreement must stop a deploy. A clause demoted into the
    `||` group would still be "in" the condition and no longer required by it."""
    deploy_job = WORKFLOW.read_text(encoding="utf-8").split("\n  deploy:\n", 1)[1]
    needs = re.search(r"^ {4}needs: \[([^\]]*)\]", deploy_job, re.M)
    assert needs, "the deploy job must keep a one-line `needs:` list"
    assert "contract-parity" in [name.strip() for name in needs.group(1).split(",")]
    assert "needs.contract-parity.result == 'success'" in _deploy_gates()


# ── Runner image and action runtimes (agent-forge-harness-7q6) ───────────────


def test_every_job_pins_its_runner_image():
    """`ubuntu-latest` moves to a new Ubuntu on GitHub's schedule, not ours, and
    this workflow deploys master. Every job names the image it was verified on."""
    jobs = _jobs()
    assert {"python-tests", "ui-tests", "ui-e2e", "deploy"} <= set(jobs), (
        f"job parsing is broken: found {sorted(jobs)}"
    )
    runners = {name: re.findall(r"^ {4}runs-on:\s*(.+?)\s*$", block, re.M) for name, block in jobs.items()}
    unpinned = {name: found for name, found in runners.items() if found != [RUNNER]}
    assert not unpinned, f"every job must set `runs-on: {RUNNER}`; these do not: {unpinned}"


def test_every_action_is_on_a_node24_major():
    """Node.js 20 actions are deprecated on GitHub-hosted runners. Every `uses:`
    names an owner/repo@v<major> at or above that action's first node24 major.

    The regex allows an optional trailing comment (`uses: x@v4  # v4`, or a
    future SHA-pin-with-a-version-comment) so such a line is still parsed and
    checked, never silently skipped, and the count is checked exactly against
    every `uses:` occurrence in the file -- `>= len(FIRST_NODE24_MAJOR)` is a
    weak floor that a line dropped from parsing (12 vs 15 seen) cannot fail."""
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#.*)?\s*$", text, re.M)
    assert len(uses) == text.count("uses:"), f"`uses:` parsing is broken: found {uses}"
    stale: list[str] = []
    for ref in uses:
        parsed = re.fullmatch(r"([\w.-]+/[\w.-]+)@v(\d+)(?:\.\d+)*", ref)
        assert parsed, f"{ref}: expected owner/repo@v<major>, so its runtime can be checked"
        action, major = parsed.group(1), int(parsed.group(2))
        assert action in FIRST_NODE24_MAJOR, (
            f"{action} is not in FIRST_NODE24_MAJOR: check which of its majors has "
            "`runs.using: node24` in action.yml and add it"
        )
        if major < FIRST_NODE24_MAJOR[action]:
            stale.append(f"{ref} (node24 from v{FIRST_NODE24_MAJOR[action]})")
    assert not stale, f"these actions still run on a deprecated Node.js: {stale}"
