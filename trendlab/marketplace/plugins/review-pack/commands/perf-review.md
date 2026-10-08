---
description: Performance review of the uncommitted diff (read-only)
---
Review the uncommitted changes in this repository (run `git diff` and `git diff --cached`) for
performance problems only: work inside loops that could move out, repeated I/O or queries,
quadratic algorithms on growing inputs, unbounded memory, blocking calls in async code. Do not
edit files. For each finding give file:line, the cost, and the smallest fix. Say plainly if
there are none.
$ARGUMENTS
