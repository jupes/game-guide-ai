# Root conftest.py — anchors pytest's rootdir to the repo root so test discovery is
# stable regardless of the invoking cwd. The actual sys.path entry that makes
# `import service` / `import ingestion` resolve without an install is the
# `[tool.pytest.ini_options] pythonpath = ["."]` setting in pyproject.toml.

import os

# /docs, /redoc and /openapi.json are off unless RAG_API_DOCS_ENABLED is set
# (agent-forge-harness-ust7). The route-census tests pin the app's whole route
# table, FastAPI's documentation routes included, so the suite runs with them
# on. Set here, before anything imports `service.app`, which reads it once;
# service/tests/test_body_limit.py checks the default (off) in a fresh
# interpreter without it.
os.environ.setdefault("RAG_API_DOCS_ENABLED", "1")

# The canary leak-test harness's fixtures, for every test directory (agent-forge-harness-1ir.1.10).
pytest_plugins = ["service.tests.canary.plugin"]
