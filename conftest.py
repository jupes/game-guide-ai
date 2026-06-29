# Root conftest.py — anchors pytest's rootdir to the repo root so test discovery is
# stable regardless of the invoking cwd. The actual sys.path entry that makes
# `import service` / `import ingestion` resolve without an install is the
# `[tool.pytest.ini_options] pythonpath = ["."]` setting in pyproject.toml.
