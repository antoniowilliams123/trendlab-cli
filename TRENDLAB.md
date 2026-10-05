# TrendLab Project Instructions

- Python 3.12; dependencies live in `.venv` (created with `python3 -m venv --without-pip`,
  installed via `pip3 --python .venv/bin/python install -e .[dev]`).
- Run `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check . && .venv/bin/ruff format .`
  after every change.
- Never route a model-proposed command around `ToolRuntime` → `PermissionEngine` → `ApprovalManager`.
- Anything shown to a remote device or written to logs must pass through `trendlab.security.redaction`.
