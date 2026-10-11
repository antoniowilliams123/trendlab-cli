# TrendLab Project Instructions

- Python 3.12; dependencies live in `.venv` (created with `python3 -m venv --without-pip`,
  installed via `pip3 --python .venv/bin/python install -e .[dev]`).
- Run `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check . && .venv/bin/ruff format .`
  after every change.
- Never route a model-proposed command around `ToolRuntime` → `PermissionEngine` → `ApprovalManager`.
- Anything shown to a remote device or written to logs must pass through `trendlab.security.redaction`.

## Learned 2026-10-11
Always attempt relevant tool calls before answering or refusing; do not say you cannot answer without trying.
When a step plan is provided, follow it in order and mark steps complete with `task(action='complete', task_id=..., evidence=...)` only after validation passes.
If a time budget is exceeded, finish the smallest complete version and report done/left.
Run validation commands from the project root so `trendlab` imports resolve.
Do not re-plan unless a step is impossible.
