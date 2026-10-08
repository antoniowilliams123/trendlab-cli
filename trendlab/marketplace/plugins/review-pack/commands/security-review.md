---
description: Security review of the uncommitted diff (read-only)
---
Review the uncommitted changes in this repository (run `git diff` and `git diff --cached`) for
security problems only: injection, path traversal, unsafe deserialisation, secrets in code or
logs, missing authorisation checks, unsafe subprocess or shell use. Do not edit files. For each
finding give file:line, what can go wrong, and the smallest fix. Say plainly if you find none.
$ARGUMENTS
